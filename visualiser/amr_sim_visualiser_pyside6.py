import argparse
import ast
import copy
import csv
from bisect import bisect_right
import json
import math
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dxf_scene import DXFScene

from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPen,
    QPolygonF,
    QFont,
    QPainterPath,
    QMouseEvent,
    QSurfaceFormat,
    QFontDatabase,
    QPixmap,
)
from PySide6.QtCore import QPointF, QTimer, Qt, QRectF, QRect, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QProgressDialog,
    QGraphicsPathItem,
    QDialog,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QScrollArea,
    QSplitter,
    QSizePolicy,
    QMenu,
    QListWidgetItem,
    QListWidget,
    QFrame,
    QLineEdit,
)

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception:  # pragma: no cover - OpenGL may be unavailable on some systems
    QOpenGLWidget = None

try:
    import ezdxf
except Exception:  # pragma: no cover
    ezdxf = None


@dataclass
class VisualEvent:
    start_time: datetime
    end_time: datetime
    row: dict


class LayoutModel:
    def __init__(self):
        self.data: dict = {}
        self.points: Dict[str, dict] = {}
        self.task_start_time: Optional[datetime] = None
        self.task_end_time: Optional[datetime] = None
        self.simulation_start_time: Optional[datetime] = None
        self.simulation_end_time: Optional[datetime] = None

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        value = (value or "").strip()
        if not value:
            return None

        candidates = [value, value.replace("Z", "+00:00")]
        for candidate in candidates:
            try:
                return datetime.fromisoformat(candidate)
            except Exception:
                continue

        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
        ]:
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue

        return None

    def _rebuild_task_timeline(self):
        times = []
        for task in self.data.get("tasks", []):
            dt = self._parse_datetime(task.get("release_datetime", ""))
            if dt is not None:
                times.append(dt)

        if times:
            self.task_start_time = min(times)
            self.task_end_time = max(times)
        else:
            self.task_start_time = None
            self.task_end_time = None

    def _rebuild_simulation_timeline(self):
        simulation = self.data.get("simulation", {}) or {}
        self.simulation_start_time = self._parse_datetime(
            simulation.get("start_datetime", "")
            or simulation.get("start_time", "")
            or simulation.get("sim_start", "")
        )
        self.simulation_end_time = self._parse_datetime(
            simulation.get("end_datetime", "")
            or simulation.get("end_time", "")
            or simulation.get("sim_end", "")
        )

    def load(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self._rebuild_points()
        self._rebuild_task_timeline()
        self._rebuild_simulation_timeline()

    def _rebuild_points(self):
        self.points = {}
        for item in self.data.get("locations", []):
            self.points[item["name"]] = {**item, "kind": "location"}
        for item in self.data.get("corridors", {}).get("nodes", []):
            self.points[item["name"]] = {**item, "kind": "corridor_node"}
        for lift in self.data.get("lifts", []):
            for floor_str, pos in lift.get("floor_locations", {}).items():
                self.points[f"{lift['id']}-F{floor_str}"] = {
                    "name": f"{lift['id']}-F{floor_str}",
                    "floor": int(floor_str),
                    "x": pos["x"],
                    "y": pos["y"],
                    "kind": "lift_node",
                }

    def edges_for_floor(self, floor: int) -> List[dict]:
        edges = []
        for edge in self.data.get("corridors", {}).get("edges", []):
            a = self.points.get(edge["from"])
            b = self.points.get(edge["to"])
            if a and b and int(a["floor"]) == floor and int(b["floor"]) == floor:
                edges.append(edge)
        return edges

    def points_for_floor(self, floor: int) -> Dict[str, dict]:
        return {k: v for k, v in self.points.items() if int(v["floor"]) == floor}

    def floors(self) -> List[int]:
        return sorted({int(p["floor"]) for p in self.points.values()})


class SimulationLog:
    def __init__(self):
        self.events: List[VisualEvent] = []
        self._event_start_times: List[datetime] = []
        self._events_by_location: Dict[str, List[VisualEvent]] = {}
        self._location_event_start_times: Dict[str, List[datetime]] = {}
        self._events_by_lift: Dict[str, List[VisualEvent]] = {}
        self._lift_event_start_times: Dict[str, List[datetime]] = {}
        self.initial_amr_home_spaces: Dict[str, dict] = {}
        self._state_checkpoints: List[tuple] = []
        self._state_checkpoint_stride = 1000
        self._state_checkpoint_indexes: List[int] = []
        self._state_cache_key = None
        self._state_cache_value = None
        # Forward playback cursor.  Normal playback advances in time, so keep a
        # mutable state accumulator and apply only newly crossed events instead
        # of replaying historic CSV rows from a checkpoint every frame.
        self._cursor_index = 0
        self._cursor_time: Optional[datetime] = None
        self._cursor_payload = self._new_state_accumulators()
        self._cursor_valid = False
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None

    @staticmethod
    def _format_runtime(seconds: float) -> str:
        total = max(0, int(seconds))
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        value = (value or "").strip()
        if not value:
            return None
        candidates = [value, value.replace("Z", "+00:00")]
        for candidate in candidates:
            try:
                return datetime.fromisoformat(candidate)
            except Exception:
                continue
        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
        ]:
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
        return None

    @staticmethod
    def _float_or_none(value):
        try:
            return float(value) if value not in (None, "") else None
        except Exception:
            return None

    @staticmethod
    def _int_or_none(value):
        try:
            return int(float(value)) if value not in (None, "") else None
        except Exception:
            return None

    def first_travel_time(self) -> Optional[datetime]:
        travel_markers = {
            "travel",
            "move",
            "movement",
            "corridor",
            "edge",
            "lift_travel",
            "lift",
        }
        for event in self.events:
            row = event.row
            segment_type = (row.get("segment_type") or "").strip().lower()
            event_type = (row.get("event_type") or "").strip().lower()
            start_node = (row.get("start_node") or "").strip()
            end_node = (row.get("end_node") or "").strip()
            if segment_type in travel_markers or event_type in travel_markers:
                return event.start_time
            if start_node and end_node and start_node != end_node:
                return event.start_time
        return self.start_time

    @staticmethod
    def _row_to_visual_event(row: dict) -> Optional[VisualEvent]:
        event_type = str(row.get("event_type", "") or "").strip().lower()
        sim_dt = SimulationLog._parse_datetime(row.get("sim_datetime", ""))
        start_dt_raw = SimulationLog._parse_datetime(row.get("start_time", ""))
        end_dt_raw = SimulationLog._parse_datetime(row.get("end_time", ""))

        # Some zero-duration bookkeeping rows, especially third-party mass
        # collection visits, are logged with the simulation start in
        # start_time/end_time and the real event time in sim_datetime.
        prefer_sim_datetime = (
            event_type == "mass_collection_visit"
            or event_type.endswith("_generated")
            or event_type
            in {
                "task_assigned",
                "multi_stop_task_assigned",
                "charge_cycle_complete",
            }
        )

        if prefer_sim_datetime and sim_dt is not None:
            start_dt = sim_dt
            end_dt = (
                sim_dt
                if event_type == "mass_collection_visit"
                else (end_dt_raw or sim_dt)
            )
            if end_dt < start_dt:
                end_dt = start_dt
        else:
            start_dt = start_dt_raw or sim_dt
            end_dt = end_dt_raw or start_dt

        if start_dt is None or end_dt is None:
            return None
        return VisualEvent(start_time=start_dt, end_time=end_dt, row=row)

    def _rebuild_event_index(self):
        self.events.sort(key=lambda e: e.start_time)
        self._event_start_times = [e.start_time for e in self.events]
        self.start_time = self.events[0].start_time if self.events else None
        self.end_time = max((e.end_time for e in self.events), default=None)
        self._rebuild_location_event_index()
        self._rebuild_lift_event_index()
        self._rebuild_initial_amr_home_spaces()
        self._rebuild_state_checkpoints()
        self.reset_playback_cursor()
        self._state_cache_key = None
        self._state_cache_value = None

    def _rebuild_initial_amr_home_spaces(self):
        self.initial_amr_home_spaces = {}
        for event in self.events:
            row = event.row
            event_type = str(row.get("event_type", "") or "").strip()
            if event_type != "initial_amr_charging_location":
                continue
            amr_id = str(row.get("amr_id", "") or "").strip()
            space_name = str(row.get("amr_inventory_space", "") or "").strip()
            location_name = (
                str(row.get("to_location", "") or "").strip()
                or str(row.get("from_location", "") or "").strip()
                or str(row.get("end_node", "") or "").strip()
                or str(row.get("start_node", "") or "").strip()
            )
            if not amr_id or not location_name:
                continue
            self.initial_amr_home_spaces[amr_id] = {
                "location": location_name,
                "space": space_name,
                "x": self._float_or_none(row.get("end_x"))
                if self._float_or_none(row.get("end_x")) is not None
                else self._float_or_none(row.get("start_x")),
                "y": self._float_or_none(row.get("end_y"))
                if self._float_or_none(row.get("end_y")) is not None
                else self._float_or_none(row.get("start_y")),
                "rotation_deg": self._float_or_none(row.get("amr_rotation_deg")),
            }

    def event_index_at(self, current_time: datetime) -> int:
        if not self.events or current_time is None:
            return 0
        return bisect_right(self._event_start_times, current_time)

    def iter_events_until(self, current_time: datetime):
        idx = self.event_index_at(current_time)
        return iter(self.events[:idx])

    def events_until(self, current_time: datetime) -> List[VisualEvent]:
        # Kept for older call sites, but new hot paths avoid repeatedly slicing
        # the full event history by using event_index_at()/location indexes.
        if not self.events or current_time is None:
            return []
        idx = self.event_index_at(current_time)
        return self.events[:idx]

    def _event_location_keys(self, row: dict) -> set:
        keys = set()
        for key in (
            "to_location",
            "dropoff",
            "end_node",
            "destination",
            "location",
            "from_location",
            "pickup",
            "start_node",
            "origin",
        ):
            value = str(row.get(key, "") or "").strip()
            if value:
                keys.add(value)
        return keys

    def _rebuild_location_event_index(self):
        self._events_by_location = {}
        self._location_event_start_times = {}
        for event in self.events:
            for location_name in self._event_location_keys(event.row):
                self._events_by_location.setdefault(location_name, []).append(event)
        for location_name, events in self._events_by_location.items():
            events.sort(key=lambda e: e.start_time)
            self._location_event_start_times[location_name] = [
                e.start_time for e in events
            ]

    def location_event_index_at(self, location_name: str, current_time: datetime) -> int:
        location_name = str(location_name or "").strip()
        if not location_name or current_time is None:
            return 0
        starts = self._location_event_start_times.get(location_name, [])
        return bisect_right(starts, current_time)

    def events_for_location_until(
        self, location_name: str, current_time: datetime
    ) -> List[VisualEvent]:
        location_name = str(location_name or "").strip()
        if not location_name or current_time is None:
            return []
        events = self._events_by_location.get(location_name, [])
        idx = self.location_event_index_at(location_name, current_time)
        return events[:idx]

    def _event_lift_keys(self, row: dict) -> set:
        """Return lift ids referenced by a CSV row for fast lift monitor rebuilds."""
        keys = set()
        lift_id = str(row.get("lift_id", "") or "").strip()
        if lift_id:
            keys.add(lift_id)

        for field in (
            "start_node",
            "end_node",
            "from_location",
            "to_location",
            "location",
        ):
            value = str(row.get(field, "") or "").strip()
            if not value:
                continue
            lower = value.lower()
            marker = lower.rfind("-f")
            if marker > 0:
                keys.add(value[:marker])
        return keys

    def _rebuild_lift_event_index(self):
        self._events_by_lift = {}
        self._lift_event_start_times = {}
        for event in self.events:
            for lift_id in self._event_lift_keys(event.row):
                self._events_by_lift.setdefault(lift_id, []).append(event)
        for lift_id, events in self._events_by_lift.items():
            events.sort(key=lambda e: e.start_time)
            self._lift_event_start_times[lift_id] = [e.start_time for e in events]

    def events_for_lift_until(
        self, lift_id: str, current_time: datetime
    ) -> List[VisualEvent]:
        lift_id = str(lift_id or "").strip()
        if not lift_id or current_time is None:
            return []
        events = self._events_by_lift.get(lift_id, [])
        starts = self._lift_event_start_times.get(lift_id, [])
        idx = bisect_right(starts, current_time)
        return events[:idx]

    def _new_state_accumulators(self):
        return {}, [], {}, {}

    def _copy_state_accumulators(self, payload):
        amr_states, recent_events, current_task_start_by_amr, last_task_id_by_amr = (
            payload
        )
        return (
            copy.deepcopy(amr_states),
            list(recent_events),
            dict(current_task_start_by_amr),
            dict(last_task_id_by_amr),
        )

    def reset_playback_cursor(self) -> None:
        """Reset the incremental state cursor used by forward playback.

        Seeking backwards, loading a new CSV, or jumping to an arbitrary task uses
        the checkpoint path again.  The next forward playback frame will seed the
        cursor from the nearest checkpoint and then continue incrementally.
        """
        self._cursor_index = 0
        self._cursor_time = None
        self._cursor_payload = self._new_state_accumulators()
        self._cursor_valid = False
        self._state_cache_key = None
        self._state_cache_value = None

    def _state_result_from_payload(self, payload, current_time: datetime):
        amr_states, recent_events, _current_task_start_by_amr, _last_task_id_by_amr = payload
        for state in amr_states.values():
            self._refresh_state_position(state, current_time)
        return amr_states, recent_events[-12:]

    def _seed_cursor_from_checkpoint(self, idx: int):
        checkpoint_idx, payload = self._checkpoint_for_index(idx)
        self._cursor_index = checkpoint_idx
        self._cursor_time = None
        self._cursor_payload = payload
        self._cursor_valid = True

    def _apply_events_to_cursor(self, target_idx: int, current_time: datetime):
        if not self._cursor_valid or target_idx < self._cursor_index:
            self._seed_cursor_from_checkpoint(target_idx)

        amr_states, recent_events, current_task_start_by_amr, last_task_id_by_amr = (
            self._cursor_payload
        )
        for event in self.events[self._cursor_index:target_idx]:
            self._apply_state_event(
                event,
                current_time,
                amr_states,
                recent_events,
                current_task_start_by_amr,
                last_task_id_by_amr,
            )

        # Keep the recent-event buffer bounded during long playback sessions.
        if len(recent_events) > 64:
            del recent_events[:-64]

        self._cursor_index = target_idx
        self._cursor_time = current_time
        return self._cursor_payload

    def _apply_state_event(
        self,
        event: VisualEvent,
        current_time: datetime,
        amr_states: Dict[str, dict],
        recent_events: List[dict],
        current_task_start_by_amr: Dict[str, datetime],
        last_task_id_by_amr: Dict[str, str],
    ) -> None:
        row = event.row
        amr_id = (row.get("amr_id") or "").strip()
        if not amr_id:
            recent_events.append(
                {"timestamp": min(current_time, event.end_time), "row": row}
            )
            return

        task_id = (row.get("task_id") or "").strip()
        payload = (row.get("payload") or "").strip()
        event_type = (row.get("event_type") or "").strip()
        segment_type = (row.get("segment_type") or "").strip()
        status = (row.get("status") or "").strip()
        start_node = (row.get("start_node") or "").strip()
        end_node = (row.get("end_node") or "").strip()
        from_location = (row.get("from_location") or "").strip()
        to_location = (row.get("to_location") or "").strip()
        start_dt = event.start_time
        end_dt = (
            event.end_time if event.end_time >= event.start_time else event.start_time
        )

        if task_id:
            previous_task_id = last_task_id_by_amr.get(amr_id)
            if previous_task_id != task_id:
                current_task_start_by_amr[amr_id] = start_dt
                last_task_id_by_amr[amr_id] = task_id
        else:
            current_task_start_by_amr.pop(amr_id, None)
            last_task_id_by_amr.pop(amr_id, None)

        state = amr_states.get(amr_id, {"amr_id": amr_id})
        state.update(
            {
                "task_id": task_id,
                "payload": payload,
                "event_type": event_type,
                "segment_type": segment_type,
                "status": status,
                "timestamp": min(current_time, end_dt),
                "start_time": start_dt,
                "end_time": end_dt,
                "start_node": start_node,
                "end_node": end_node,
                "from_location": from_location,
                "to_location": to_location,
                "raw": row,
                "amr_inventory_space": (row.get("amr_inventory_space") or "").strip(),
                "amr_rotation_deg": self._float_or_none(row.get("amr_rotation_deg")),
                "amr_rotation_start_deg": self._float_or_none(row.get("amr_rotation_start_deg")),
                "amr_rotation_end_deg": self._float_or_none(row.get("amr_rotation_end_deg")),
                "_assignment_start": current_task_start_by_amr.get(amr_id, start_dt),
            }
        )
        self._refresh_state_position(state, current_time)
        amr_states[amr_id] = state
        recent_events.append({"timestamp": min(current_time, end_dt), "row": row})

    def _refresh_state_position(self, state: dict, current_time: datetime) -> None:
        row = state.get("raw", {}) or {}
        start_x = self._float_or_none(row.get("start_x"))
        start_y = self._float_or_none(row.get("start_y"))
        start_floor = self._int_or_none(row.get("start_floor"))
        end_x = self._float_or_none(row.get("end_x"))
        end_y = self._float_or_none(row.get("end_y"))
        end_floor = self._int_or_none(row.get("end_floor"))
        start_node = (row.get("start_node") or "").strip()
        end_node = (row.get("end_node") or "").strip()
        start_dt = state.get("start_time")
        end_dt = state.get("end_time")
        if start_dt is None or end_dt is None:
            return
        if start_dt <= current_time <= end_dt:
            total = max((end_dt - start_dt).total_seconds(), 0.001)
            elapsed = max((current_time - start_dt).total_seconds(), 0.0)
            frac = max(0.0, min(1.0, elapsed / total))
            state["segment_fraction"] = frac
            if (
                start_x is not None
                and start_y is not None
                and end_x is not None
                and end_y is not None
            ):
                state["x"] = start_x + ((end_x - start_x) * frac)
                state["y"] = start_y + ((end_y - start_y) * frac)
            else:
                state["x"] = end_x if end_x is not None else start_x
                state["y"] = end_y if end_y is not None else start_y
            if start_floor is not None and end_floor is not None:
                state["floor"] = start_floor if frac < 1.0 else end_floor
            elif end_floor is not None:
                state["floor"] = end_floor
            elif start_floor is not None:
                state["floor"] = start_floor
            state["path"] = (start_node, end_node) if start_node and end_node else None
        else:
            state["x"] = end_x if end_x is not None else start_x
            state["y"] = end_y if end_y is not None else start_y
            state["floor"] = end_floor if end_floor is not None else start_floor
            state["path"] = None
            state["segment_fraction"] = 1.0
        task_id = state.get("task_id")
        assignment_start = state.get("_assignment_start") or start_dt
        state["task_runtime_sec"] = (
            max((current_time - assignment_start).total_seconds(), 0.0)
            if task_id
            else 0.0
        )
        state["timestamp"] = min(current_time, end_dt)

    def _rebuild_state_checkpoints(self):
        self._state_checkpoints = []
        self._state_checkpoint_indexes = []
        if not self.events:
            return
        amr_states, recent_events, current_task_start_by_amr, last_task_id_by_amr = (
            self._new_state_accumulators()
        )
        self._state_checkpoints.append(
            (
                0,
                self._copy_state_accumulators(
                    (
                        amr_states,
                        recent_events[-12:],
                        current_task_start_by_amr,
                        last_task_id_by_amr,
                    )
                ),
            )
        )
        self._state_checkpoint_indexes.append(0)
        for idx, event in enumerate(self.events, start=1):
            self._apply_state_event(
                event,
                (
                    event.end_time
                    if event.end_time >= event.start_time
                    else event.start_time
                ),
                amr_states,
                recent_events,
                current_task_start_by_amr,
                last_task_id_by_amr,
            )
            if idx % self._state_checkpoint_stride == 0:
                self._state_checkpoints.append(
                    (
                        idx,
                        self._copy_state_accumulators(
                            (
                                amr_states,
                                recent_events[-12:],
                                current_task_start_by_amr,
                                last_task_id_by_amr,
                            )
                        ),
                    )
                )
                self._state_checkpoint_indexes.append(idx)

    def _checkpoint_for_index(self, idx: int):
        if not self._state_checkpoints:
            return 0, self._new_state_accumulators()
        checkpoint_indexes = self._state_checkpoint_indexes or [item[0] for item in self._state_checkpoints]
        pos = max(0, bisect_right(checkpoint_indexes, idx) - 1)
        checkpoint_idx, payload = self._state_checkpoints[pos]
        return checkpoint_idx, self._copy_state_accumulators(payload)

    def load(self, path: str):
        self.events = []
        self._event_start_times = []
        chunk_size = 10000
        temp_dir = None
        chunk_paths: List[str] = []
        total_rows = 0

        try:
            temp_parent = Path(path).resolve().parent
            temp_path = temp_parent / (
                f"amr_visualiser_events_{os.getpid()}_{int(time.time() * 1000)}"
            )
            suffix = 0
            while temp_path.exists():
                suffix += 1
                temp_path = temp_parent / (
                    f"amr_visualiser_events_{os.getpid()}_{int(time.time() * 1000)}_{suffix}"
                )
            temp_path.mkdir(parents=True, exist_ok=False)
            temp_dir = str(temp_path)
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                writer = None
                chunk_file = None
                chunk_row_count = 0

                for row in reader:
                    if writer is None or chunk_row_count >= chunk_size:
                        if chunk_file is not None:
                            chunk_file.close()
                        chunk_path = os.path.join(
                            temp_dir, f"events_{len(chunk_paths):05d}.csv"
                        )
                        chunk_file = open(
                            chunk_path, "w", encoding="utf-8", newline=""
                        )
                        writer = csv.DictWriter(
                            chunk_file, fieldnames=fieldnames, extrasaction="ignore"
                        )
                        writer.writeheader()
                        chunk_paths.append(chunk_path)
                        chunk_row_count = 0
                    writer.writerow(row)
                    chunk_row_count += 1
                    total_rows += 1

                if chunk_file is not None:
                    chunk_file.close()

            # Parse timestamp-heavy CSV chunks in worker processes for large logs.
            # Qt objects are not touched here; only plain dicts and datetimes are returned.
            if total_rows >= 5000 and chunk_paths:
                workers = min(max(1, (os.cpu_count() or 2) - 1), 8, len(chunk_paths))
                try:
                    with ProcessPoolExecutor(max_workers=workers) as pool:
                        for events in pool.map(
                            _parse_visual_event_chunk_file_process, chunk_paths
                        ):
                            self.events.extend(events)
                except Exception:
                    self.events = []
                    for chunk_path in chunk_paths:
                        self.events.extend(
                            _parse_visual_event_chunk_file_process(chunk_path)
                        )
            else:
                for chunk_path in chunk_paths:
                    self.events.extend(_parse_visual_event_chunk_file_process(chunk_path))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        self._rebuild_event_index()

    def fraction_to_time(self, fraction: float) -> Optional[datetime]:
        if not self.start_time or not self.end_time:
            return None
        fraction = max(0.0, min(1.0, fraction))
        span = self.end_time - self.start_time
        return self.start_time + (span * fraction)

    def time_to_fraction(self, value: datetime) -> float:
        if not self.start_time or not self.end_time or self.start_time == self.end_time:
            return 0.0
        return max(
            0.0,
            min(
                1.0,
                (value - self.start_time).total_seconds()
                / (self.end_time - self.start_time).total_seconds(),
            ),
        )

    def state_at(self, current_time: datetime, layout: LayoutModel):
        if not self.events or current_time is None:
            return {}, []

        idx = self.event_index_at(current_time)
        cache_key = (idx, current_time)
        if self._state_cache_key == cache_key and self._state_cache_value is not None:
            return self._state_cache_value

        # Normal playback moves forward.  Apply only new events since the last
        # frame; do not rebuild from a historic checkpoint on every tick.
        if (
            self._cursor_valid
            and self._cursor_time is not None
            and current_time >= self._cursor_time
            and idx >= self._cursor_index
        ):
            payload = self._apply_events_to_cursor(idx, current_time)
            result = self._state_result_from_payload(payload, current_time)
        else:
            self._seed_cursor_from_checkpoint(idx)
            payload = self._apply_events_to_cursor(idx, current_time)
            result = self._state_result_from_payload(payload, current_time)

        self._state_cache_key = cache_key
        self._state_cache_value = result
        return result


class GraphicsView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_pan_pos = None
        self._zoom_callback = None
        self._pan_callback = None
        self._overlay_provider = None
        self._context_menu_callback = None

        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.TextAntialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setCacheMode(QGraphicsView.CacheBackground)
        self.setBackgroundBrush(QBrush(QColor("#111111")))

        self.opengl_enabled = False
        self.opengl_error = ""
        self.graphics_backend = "raster"
        self.enable_opengl_viewport()

    def enable_opengl_viewport(self) -> bool:
        """Use an OpenGL-backed viewport when available.

        The visualiser still uses QGraphicsScene/QGraphicsItem so behaviour,
        picking and dialogs remain unchanged, but the final paint surface is
        GPU-backed. Heavy parsing/calculation remains in worker processes; Qt
        widgets and QGraphicsItems stay in the GUI process.
        """
        if QOpenGLWidget is None:
            self.opengl_error = "PySide6.QtOpenGLWidgets is not available"
            return False

        try:
            fmt = QSurfaceFormat()
            fmt.setDepthBufferSize(24)
            fmt.setStencilBufferSize(8)
            fmt.setSamples(4)

            viewport = QOpenGLWidget(self)
            viewport.setFormat(fmt)
            self.setViewport(viewport)

            # With an OpenGL viewport partial viewport updates can leave stale
            # regions on some drivers. Full updates are more stable and the GPU
            # handles the compositing work.
            self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
            self.opengl_enabled = True
            self.graphics_backend = "opengl"
            self.opengl_error = ""
            return True
        except Exception as exc:  # pragma: no cover - driver/platform dependent
            self.opengl_enabled = False
            self.graphics_backend = "raster"
            self.opengl_error = str(exc)
            self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
            return False

    def set_callbacks(self, zoom_callback=None, pan_callback=None):
        self._zoom_callback = zoom_callback
        self._pan_callback = pan_callback

    def set_context_menu_callback(self, context_menu_callback):
        self._context_menu_callback = context_menu_callback

    def set_overlay_provider(self, overlay_provider):
        self._overlay_provider = overlay_provider
        self.viewport().update()

    def wheelEvent(self, event):
        factor = 1.1 if event.angleDelta().y() > 0 else 0.9
        self.scale(factor, factor)
        if self._zoom_callback:
            self._zoom_callback()
        self.viewport().update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._last_pan_pos = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.RightButton and self._context_menu_callback:
            self._context_menu_callback(event)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._last_pan_pos is not None:
            delta = event.position() - self._last_pan_pos
            self._last_pan_pos = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            if self._pan_callback:
                self._pan_callback()
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._last_pan_pos = None
            self.setCursor(Qt.ArrowCursor)
            self.viewport().update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self._overlay_provider:
            painter.save()
            painter.resetTransform()
            self._overlay_provider(painter, self.viewport().rect())
            painter.restore()


def _load_dxf_floor_process(job):
    """Load one DXF in a worker process and return serialisable content.

    This mirrors the editor: DXF parsing happens outside the GUI thread and
    only plain entity/bounds data is passed back to Qt. QGraphicsItems are
    created later, on demand, for the active floor only.
    """
    floor, path = job
    floor = int(floor)
    path = str(path or "").strip()

    try:
        if not path or not Path(path).exists():
            return {
                "ok": False,
                "floor": floor,
                "path": path,
                "entities": None,
                "bounds": None,
                "error": f"DXF file not found: {path}",
            }

        payload = DXFScene.load_content(path)
        return {
            "ok": True,
            "floor": floor,
            "path": path,
            "entities": payload.get("entities", []),
            "bounds": payload.get("bounds"),
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "floor": floor,
            "path": path,
            "entities": None,
            "bounds": None,
            "error": str(exc),
        }


def _parse_visual_event_row_process(row):
    """Process-safe CSV row parser used by SimulationLog.load for large logs."""
    return SimulationLog._row_to_visual_event(row)


def _parse_visual_event_chunk_file_process(path):
    """Parse one temporary CSV chunk in a worker process."""
    events = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            event = SimulationLog._row_to_visual_event(row)
            if event is not None:
                events.append(event)
    return events


class DxfLoadWorker(QObject):
    progress = Signal(int, int, str)
    floor_loaded = Signal(int, str, object, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(self, floor_dxf_files):
        super().__init__()
        self.floor_dxf_files = list(floor_dxf_files)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _normalise_jobs(self):
        jobs = []
        seen = set()

        for entry in self.floor_dxf_files:
            try:
                floor = int(entry.get("floor"))
                path = str(entry.get("filepath") or "").strip()
            except Exception:
                continue

            if not path:
                continue

            key = (floor, path)
            if key in seen:
                continue

            seen.add(key)
            jobs.append(key)

        return jobs

    def run(self):
        jobs = self._normalise_jobs()
        total = len(jobs)

        if total <= 0:
            self.finished.emit()
            return

        worker_count = min(total, max(1, (os.cpu_count() or 2) - 1))
        completed = 0

        self.progress.emit(0, total, "Preparing DXF load...")

        try:
            with ProcessPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_load_dxf_floor_process, job) for job in jobs]

                for future in as_completed(futures):
                    completed += 1

                    if self._cancelled:
                        continue

                    result = future.result()
                    floor = int(result.get("floor", 0))
                    path = str(result.get("path", ""))
                    label = Path(path).name if path else ""

                    self.progress.emit(
                        completed,
                        total,
                        f"Loaded {completed} of {total} DXFs...\n{label}",
                    )

                    if result.get("ok"):
                        self.floor_loaded.emit(
                            floor,
                            path,
                            result.get("entities") or [],
                            result.get("bounds"),
                        )
                    else:
                        self.error.emit(
                            floor,
                            str(result.get("error") or "Unknown DXF load error"),
                        )
        finally:
            self.finished.emit()


class TaskJumpDialog(QDialog):
    def __init__(self, parent, grouped_tasks):
        super().__init__(parent)
        self.setWindowTitle("Tasks by AMR")
        self.resize(980, 620)
        self.selected_start_time = None
        self.selected_amr_id = None

        self._sort_column = None
        self._sort_state = 0  # 0=original, 1=asc, 2=desc
        self._insertion_counter = 0

        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(
            [
                "Task ID / Segment",
                "Payload",
                "Origin",
                "Destination",
                "Duration",
                "Datetime",
            ]
        )
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.header().sectionClicked.connect(self._on_header_clicked)

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        for amr_id in sorted(grouped_tasks.keys()):
            amr_item = QTreeWidgetItem([amr_id, "", "", "", "", ""])
            amr_item.setFirstColumnSpanned(True)
            amr_item.setData(0, Qt.UserRole, None)
            amr_item.setData(1, Qt.UserRole, None)
            amr_item.setData(0, Qt.UserRole + 10, self._next_insertion_order())
            amr_item.setData(0, Qt.UserRole + 20, "amr")

            for task in grouped_tasks[amr_id]:
                task_item = QTreeWidgetItem(
                    [
                        task["task_id"],
                        task["payload"],
                        task["origin"],
                        task["destination"],
                        task["duration"],
                        task["sim_datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                    ]
                )
                task_item.setData(0, Qt.UserRole, task["start_time"])
                task_item.setData(1, Qt.UserRole, amr_id)
                task_item.setData(0, Qt.UserRole + 10, self._next_insertion_order())
                task_item.setData(0, Qt.UserRole + 20, "task")

                for segment in task.get("segments", []):
                    segment_item = QTreeWidgetItem(
                        [
                            segment["label"],
                            "",
                            segment["origin"],
                            segment["destination"],
                            segment["duration"],
                            segment["sim_datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        ]
                    )
                    segment_item.setData(0, Qt.UserRole, segment["start_time"])
                    segment_item.setData(1, Qt.UserRole, amr_id)
                    segment_item.setData(
                        0, Qt.UserRole + 10, self._next_insertion_order()
                    )
                    segment_item.setData(0, Qt.UserRole + 20, "segment")
                    task_item.addChild(segment_item)

                task_item.setExpanded(False)
                amr_item.addChild(task_item)

            amr_item.setExpanded(True)
            self.tree.addTopLevelItem(amr_item)

        layout.addWidget(self.tree)

    def _next_insertion_order(self) -> int:
        value = self._insertion_counter
        self._insertion_counter += 1
        return value

    def _on_item_double_clicked(self, item, _column):
        start_time = item.data(0, Qt.UserRole)
        amr_id = item.data(1, Qt.UserRole)

        if start_time is None:
            return

        self.selected_start_time = start_time
        self.selected_amr_id = amr_id
        self.accept()

    def _on_header_clicked(self, column: int):
        if self._sort_column != column:
            self._sort_column = column
            self._sort_state = 1
        else:
            self._sort_state = (self._sort_state + 1) % 3

        if self._sort_state == 0:
            self._restore_original_order()
        else:
            ascending = self._sort_state == 1
            self._sort_tree(column, ascending)

    def _restore_original_order(self):
        amr_items = []
        while self.tree.topLevelItemCount():
            amr_items.append(self.tree.takeTopLevelItem(0))

        amr_items.sort(key=lambda item: item.data(0, Qt.UserRole + 10))

        for amr_item in amr_items:
            self._sort_children_by_original_order(amr_item)
            self.tree.addTopLevelItem(amr_item)

    def _sort_children_by_original_order(self, parent_item: QTreeWidgetItem):
        children = []
        while parent_item.childCount():
            children.append(parent_item.takeChild(0))

        children.sort(key=lambda item: item.data(0, Qt.UserRole + 10))

        for child in children:
            self._sort_children_by_original_order(child)
            parent_item.addChild(child)

    def _sort_tree(self, column: int, ascending: bool):
        amr_items = []
        while self.tree.topLevelItemCount():
            amr_items.append(self.tree.takeTopLevelItem(0))

        amr_items.sort(
            key=lambda item: self._item_sort_key(item, column), reverse=not ascending
        )

        for amr_item in amr_items:
            self._sort_children(amr_item, column, ascending)
            self.tree.addTopLevelItem(amr_item)

    def _sort_children(
        self, parent_item: QTreeWidgetItem, column: int, ascending: bool
    ):
        children = []
        while parent_item.childCount():
            children.append(parent_item.takeChild(0))

        children.sort(
            key=lambda item: self._item_sort_key(item, column), reverse=not ascending
        )

        for child in children:
            self._sort_children(child, column, ascending)
            parent_item.addChild(child)

    def _item_sort_key(self, item: QTreeWidgetItem, column: int):
        item_type = item.data(0, Qt.UserRole + 20)

        # Keep AMR rows grouped sensibly when sorting their children
        if item_type == "amr":
            return (
                self._safe_text(item, 0).lower(),
                item.data(0, Qt.UserRole + 10),
            )

        if column == 4:
            return (
                self._duration_seconds(self._safe_text(item, 4)),
                self._safe_text(item, 0).lower(),
                item.data(0, Qt.UserRole + 10),
            )

        if column == 5:
            return (
                self._datetime_key(self._safe_text(item, 5)),
                self._safe_text(item, 0).lower(),
                item.data(0, Qt.UserRole + 10),
            )

        return (
            self._safe_text(item, column).lower(),
            self._safe_text(item, 0).lower(),
            item.data(0, Qt.UserRole + 10),
        )

    def _safe_text(self, item: QTreeWidgetItem, column: int) -> str:
        text = item.text(column)
        return text if text is not None else ""

    def _duration_seconds(self, text: str) -> int:
        parts = [p for p in text.strip().split(":") if p != ""]
        try:
            if len(parts) == 3:
                h, m, s = [int(x) for x in parts]
                return h * 3600 + m * 60 + s
            if len(parts) == 2:
                m, s = [int(x) for x in parts]
                return m * 60 + s
        except Exception:
            pass
        return -1

    def _datetime_key(self, text: str):
        try:
            return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min


class TasksByLocationDepartmentDialog(QDialog):
    """Browse full tasks grouped by department or configured location."""

    columns = [
        ("task_id", "Task", 210),
        ("payload", "Payload", 170),
        ("start_time_display", "Start time", 170),
        ("end_time_display", "End time", 170),
        ("finish_location", "Finish location", 180),
        ("department", "Department", 170),
        ("staff", "Staff", 150),
        ("people_required", "People", 80),
        ("duration", "Duration", 100),
        ("status", "Status", 110),
        ("source", "Source", 100),
    ]

    def __init__(self, parent, rows: List[dict]):
        super().__init__(parent)
        self.setWindowTitle("Tasks by location / department")
        self.resize(1420, 720)
        self.rows = list(rows or [])
        self.selected_time = None
        self.selected_task_id = ""
        self.selected_location = ""

        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Group by"))
        self.group_combo = QComboBox()
        self.group_combo.addItems(["Department", "Location"])
        self.group_combo.currentTextChanged.connect(self.refresh_tree)
        controls.addWidget(self.group_combo)

        controls.addWidget(QLabel("Filter"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            "Task, payload, department, start or finish location"
        )
        self.filter_edit.textChanged.connect(self.refresh_tree)
        controls.addWidget(self.filter_edit, 1)
        layout.addLayout(controls)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.columns))
        self.tree.setHeaderLabels([heading for _key, heading, _width in self.columns])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        header = self.tree.header()
        for idx, (_key, _heading, width) in enumerate(self.columns):
            header.setSectionResizeMode(idx, QHeaderView.Interactive)
            self.tree.setColumnWidth(idx, width)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.refresh_tree()

    def _row_text(self, row: dict) -> str:
        keys = [
            "task_id",
            "payload",
            "finish_location",
            "department",
            "department_id",
            "staff",
            "people_required",
            "status",
            "source",
            "start_time_display",
            "end_time_display",
        ]
        return " ".join(str(row.get(k, "") or "") for k in keys).lower()

    def _group_keys_for_row(self, row: dict, group_mode: str) -> List[str]:
        if group_mode == "location":
            groups = []
            for key in ("start_location", "finish_location"):
                location = str(row.get(key, "") or "").strip()
                if location and location != "-" and location not in groups:
                    groups.append(location)
            return groups or ["Unknown location"]
        dept = str(
            row.get("department", "") or row.get("department_id", "") or ""
        ).strip()
        return [dept or "Unassigned department"]

    def refresh_tree(self):
        self.tree.clear()
        group_mode = self.group_combo.currentText().strip().lower()
        filter_text = self.filter_edit.text().strip().lower()

        grouped: Dict[str, List[dict]] = {}
        for row in self.rows:
            if filter_text and filter_text not in self._row_text(row):
                continue
            for group in self._group_keys_for_row(row, group_mode):
                grouped.setdefault(group, []).append(row)

        unique_task_count = len(
            {str(r.get("task_id", "")) for rows in grouped.values() for r in rows}
        )
        self.summary_label.setText(
            f"Groups: {len(grouped)} | Full tasks: {unique_task_count} | Double-click a row to jump to its start time."
        )

        for group in sorted(grouped.keys(), key=lambda x: x.lower()):
            rows = sorted(
                grouped[group],
                key=lambda r: (
                    str(
                        r.get("start_sort_time", "") or r.get("start_time_display", "")
                    ),
                    str(r.get("task_id", "")),
                ),
            )
            group_item = QTreeWidgetItem([group] + [""] * (len(self.columns) - 1))
            group_item.setFirstColumnSpanned(True)
            group_item.setExpanded(True)
            self.tree.addTopLevelItem(group_item)

            for row in rows:
                values = [
                    str(row.get(key, "") or "-")
                    for key, _heading, _width in self.columns
                ]
                item = QTreeWidgetItem(values)
                item.setData(0, Qt.UserRole, row.get("start_time"))
                item.setData(1, Qt.UserRole, row.get("task_id", ""))
                item.setData(
                    2,
                    Qt.UserRole,
                    row.get("finish_location") or row.get("start_location") or "",
                )
                details = str(row.get("details", "") or "")
                if details:
                    for col in range(len(self.columns)):
                        item.setToolTip(col, details)
                group_item.addChild(item)

    def _on_item_double_clicked(self, item, _column):
        start_time = item.data(0, Qt.UserRole)
        if start_time is None:
            return
        self.selected_time = start_time
        self.selected_task_id = str(item.data(1, Qt.UserRole) or "")
        self.selected_location = str(item.data(2, Qt.UserRole) or "")
        self.accept()


class LiftShaftWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lift_state = None
        self.setMinimumSize(120, 260)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_lift_state(self, lift_state: dict):
        self.lift_state = lift_state
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#151515"))

        state = self.lift_state or {}
        floors = list(state.get("served_floors", []))
        if not floors:
            floors = [0]

        min_floor = min(floors)
        max_floor = max(floors)
        span = max(1, max_floor - min_floor)

        left = 44
        top = 20
        shaft_w = 32
        shaft_h = max(160, self.height() - 120)

        painter.setPen(QPen(QColor("#666666"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(left, top, shaft_w, shaft_h)

        font = QFont()
        font.setPixelSize(11)
        painter.setFont(font)

        for floor in sorted(floors):
            if span == 0:
                frac = 0.0
            else:
                frac = (floor - min_floor) / span
            y = top + shaft_h - (frac * shaft_h)
            painter.setPen(QPen(QColor("#2f2f2f"), 1))
            painter.drawLine(left - 10, int(y), left + shaft_w + 10, int(y))
            painter.setPen(QColor("#d7d7d7"))
            painter.drawText(6, int(y) + 4, f"F{floor}")

        current_floor = float(state.get("current_floor", min_floor))
        current_floor = max(min_floor, min(max_floor, current_floor))
        if span == 0:
            frac = 0.0
        else:
            frac = (current_floor - min_floor) / span
        car_h = 24
        car_y = top + shaft_h - (frac * shaft_h) - (car_h / 2)

        painter.setPen(QPen(QColor("#111111"), 1))
        painter.setBrush(QBrush(QColor("#f39c12")))
        painter.drawRect(left + 2, int(car_y), shaft_w - 4, car_h)

        occupant = state.get("occupant") or "-"
        painter.setPen(QColor("#ffffff"))
        painter.drawText(12, top + shaft_h + 26, f"AMR: {occupant}")
        painter.drawText(12, top + shaft_h + 46, f"Pos: F{current_floor:.2f}")


class LocationInventoryPayloadDialog(QDialog):
    columns = [
        ("space", "Inventory space", 180),
        ("payload", "Current payload", 170),
        ("details", "Payload details", 260),
        ("waste_volume_display", "Waste volume filled", 160),
        ("fill_percent_display", "Fill", 90),
        ("payload_instance_id", "Payload instance", 210),
        ("waste_stream", "Waste stream", 120),
        ("container_group", "Container group", 160),
        ("task_id", "Task", 100),
        ("amr_id", "AMR", 100),
        ("status", "Status", 130),
        ("timestamp", "Updated", 160),
        ("source", "Source", 150),
    ]

    def __init__(
        self,
        parent,
        location_name: str,
        rows: List[dict],
        current_time: Optional[datetime],
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Inventory payloads - {location_name}")
        self.resize(980, 420)
        self.location_name = location_name
        self.rows = list(rows or [])
        self.current_time = current_time

        layout = QVBoxLayout(self)
        stamp = current_time.strftime("%Y-%m-%d %H:%M:%S") if current_time else "-"
        self.summary_label = QLabel(
            f"Location: {location_name}\nTime: {stamp}\nSpaces: {len(self.rows)}"
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self._refresh_table()

    def _refresh_table(self):
        self.table.setRowCount(0)
        for row_data in self.rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, (key, _heading, _width) in enumerate(self.columns):
                value = row_data.get(key, "")
                self.table.setItem(
                    row,
                    col,
                    QTableWidgetItem(str(value if value not in (None, "") else "-")),
                )


class LocationInventorySpacesDialog(QDialog):
    columns = [
        ("name", "Inventory space", 180),
        ("length_m", "Length m", 90),
        ("width_m", "Width m", 90),
        ("height_m", "Height m", 90),
        ("occupied", "Occupied", 90),
        ("payload", "Payload", 160),
        ("task_id", "Task", 120),
        ("reserved_by_task", "Reserved by", 120),
        ("points", "Points", 90),
    ]

    def __init__(self, parent, location_name: str, rows: List[dict]):
        super().__init__(parent)
        self.setWindowTitle(f"Inventory spaces - {location_name}")
        self.resize(1080, 460)
        self.location_name = location_name
        self.rows = list(rows or [])

        layout = QVBoxLayout(self)
        self.summary_label = QLabel(
            f"Location: {location_name}\nInventory spaces: {len(self.rows)}"
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self._refresh_table()

    def _refresh_table(self):
        self.table.setRowCount(0)
        for row_data in self.rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, (key, _heading, _width) in enumerate(self.columns):
                value = row_data.get(key, "")
                self.table.setItem(
                    row,
                    col,
                    QTableWidgetItem(str(value if value not in (None, "") else "-")),
                )


class AmrPayloadMonitorDialog(QDialog):
    columns = [
        ("amr_id", "AMR", 120),
        ("payloads", "Payloads onboard", 260),
        ("payload_count", "Count", 70),
        ("slots", "Slots", 90),
        ("task_ids", "Task(s)", 190),
        ("status", "Status", 130),
        ("segment_type", "Segment", 130),
        ("from_location", "From", 150),
        ("to_location", "To", 150),
        ("floor", "Floor", 70),
        ("updated", "Updated", 160),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AMR Payload Monitor")
        self.setWindowModality(Qt.NonModal)
        self.resize(1320, 520)
        self._rows = []

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("No simulation loaded")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def update_states(self, rows: List[dict], current_time: Optional[datetime]):
        self._rows = list(rows or [])
        stamp = current_time.strftime("%Y-%m-%d %H:%M:%S") if current_time else "-"
        payload_total = sum(int(row.get("payload_count", 0) or 0) for row in self._rows)
        self.summary_label.setText(
            f"Time: {stamp}\nAMRs: {len(self._rows)} | Payloads onboard: {payload_total}"
        )
        self._refresh_table()

    def _refresh_table(self):
        selected_amr = ""
        selected_rows = (
            self.table.selectionModel().selectedRows()
            if self.table.selectionModel()
            else []
        )
        if selected_rows:
            item = self.table.item(selected_rows[0].row(), 0)
            selected_amr = item.text() if item else ""

        self.table.setRowCount(0)
        for row_data in self._rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, (key, _heading, _width) in enumerate(self.columns):
                value = row_data.get(key, "")
                text = str(value if value not in (None, "") else "-")
                item = QTableWidgetItem(text)
                if key == "payload_count":
                    try:
                        item.setData(Qt.DisplayRole, int(value or 0))
                    except Exception:
                        pass
                self.table.setItem(row, col, item)
            if selected_amr and str(row_data.get("amr_id", "")) == selected_amr:
                self.table.selectRow(row)


class LiftMonitorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lift Monitor")
        self.setModal(True)
        self.resize(980, 520)
        self._lift_widgets = {}

        outer = QVBoxLayout(self)
        row = QHBoxLayout()
        outer.addLayout(row)

        self._row = row

        self.setWindowModality(Qt.NonModal)

    def set_lifts(self, lift_states: List[dict]):
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._lift_widgets = {}

        for lift_state in lift_states:
            panel = QFrame()
            panel.setFrameShape(QFrame.StyledPanel)
            panel.setStyleSheet(
                "QFrame { background: #101010; border: 1px solid #333333; } QLabel { color: white; } QListWidget { background: #151515; color: white; border: 1px solid #333333; }"
            )
            layout = QVBoxLayout(panel)

            shaft = LiftShaftWidget(panel)
            waiting_label = QLabel("Waiting AMRs")
            waiting_list = QListWidget(panel)
            waiting_list.setMinimumHeight(110)
            name_label = QLabel(lift_state.get("lift_id", "Lift"))
            name_label.setAlignment(Qt.AlignCenter)

            layout.addWidget(shaft, alignment=Qt.AlignHCenter)
            layout.addWidget(name_label)
            layout.addWidget(waiting_label)
            layout.addWidget(waiting_list)

            self._row.addWidget(panel)
            self._lift_widgets[lift_state.get("lift_id", "")] = (shaft, waiting_list)

        self.update_states(lift_states)

    def update_states(self, lift_states: List[dict]):
        if set(self._lift_widgets.keys()) != {
            x.get("lift_id", "") for x in lift_states
        }:
            self.set_lifts(lift_states)
            return

        for lift_state in lift_states:
            lift_id = lift_state.get("lift_id", "")
            if lift_id not in self._lift_widgets:
                continue
            shaft, waiting_list = self._lift_widgets[lift_id]
            shaft.set_lift_state(lift_state)
            waiting_list.clear()
            waiting = lift_state.get("waiting_amrs", [])
            if waiting:
                for amr in waiting:
                    waiting_list.addItem(QListWidgetItem(amr))
            else:
                waiting_list.addItem(QListWidgetItem("-"))


class AmrTimelineWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timeline_data = []
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.current_time: Optional[datetime] = None

        self.row_height = 34
        self.left_pad = 150
        self.top_pad = 58
        self.right_pad = 60
        self.bottom_pad = 30

        # Long simulations can span several days.  The lane remains horizontally
        # scrollable, while the AMR name column is redrawn at the visible left
        # edge so the lane identity is always readable.
        self.default_seconds_per_pixel = 4.0
        self.seconds_per_pixel = self.default_seconds_per_pixel
        self.min_seconds_per_pixel = 0.25
        self.max_seconds_per_pixel = 3600.0
        self.min_lane_width = 1400
        self.label_column_width = 142
        self.min_tick_spacing_px = 120
        self._pressed = False

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._update_virtual_size()

    def set_zoom_seconds_per_pixel(self, seconds_per_pixel: float):
        try:
            value = float(seconds_per_pixel)
        except Exception:
            value = self.default_seconds_per_pixel
        self.seconds_per_pixel = max(
            self.min_seconds_per_pixel,
            min(self.max_seconds_per_pixel, value),
        )
        self._update_virtual_size()
        self.update()

    def zoom_by_factor(self, factor: float, anchor_time: Optional[datetime] = None):
        try:
            factor = float(factor)
        except Exception:
            factor = 1.0
        if factor <= 0:
            factor = 1.0
        self.set_zoom_seconds_per_pixel(self.seconds_per_pixel * factor)
        parent = self.parent()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parent()
        if anchor_time is not None and parent is not None:
            x = self._time_to_x(anchor_time)
            bar = parent.horizontalScrollBar()
            bar.setValue(max(0, int(x - (parent.viewport().width() / 2))))

    def set_data(self, timeline_data, start_time, end_time, current_time):
        self.timeline_data = timeline_data or []
        self.start_time = start_time
        self.end_time = end_time
        self.current_time = current_time
        self._update_virtual_size()
        self.update()

    def _timeline_seconds(self) -> float:
        if not self.start_time or not self.end_time or self.end_time <= self.start_time:
            return 0.0
        return max(0.0, (self.end_time - self.start_time).total_seconds())

    def _usable_width(self) -> float:
        seconds = self._timeline_seconds()
        if seconds <= 0:
            return self.min_lane_width
        return max(self.min_lane_width, seconds / self.seconds_per_pixel)

    def _update_virtual_size(self):
        lane_count = max(1, len(self.timeline_data))
        width = int(self.left_pad + self._usable_width() + self.right_pad)
        height = int(
            self.top_pad + self.bottom_pad + (lane_count * self.row_height) + 20
        )
        self.setMinimumSize(width, height)
        self.resize(width, height)

    def _time_to_x(self, value: datetime) -> float:
        if not self.start_time or not self.end_time or self.end_time <= self.start_time:
            return float(self.left_pad)

        total = (self.end_time - self.start_time).total_seconds()
        elapsed = (value - self.start_time).total_seconds()
        frac = max(0.0, min(1.0, elapsed / total))
        return self.left_pad + (self._usable_width() * frac)

    def _x_to_time(self, x: float) -> Optional[datetime]:
        if not self.start_time or not self.end_time or self.end_time <= self.start_time:
            return None

        frac = (x - self.left_pad) / max(1.0, self._usable_width())
        frac = max(0.0, min(1.0, frac))
        span = self.end_time - self.start_time
        return self.start_time + (span * frac)

    def _format_datetime(self, value: Optional[datetime]) -> str:
        if value is None:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _visible_scroll_x(self) -> int:
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return int(parent.horizontalScrollBar().value())
            parent = parent.parent()
        return 0

    def _nice_tick_seconds(self) -> int:
        seconds = max(1.0, self._timeline_seconds())
        approximate_ticks = max(
            2, int(self._usable_width() // self.min_tick_spacing_px)
        )
        raw_step = seconds / approximate_ticks
        candidates = [
            60,
            5 * 60,
            10 * 60,
            15 * 60,
            30 * 60,
            60 * 60,
            2 * 60 * 60,
            3 * 60 * 60,
            6 * 60 * 60,
            12 * 60 * 60,
            24 * 60 * 60,
            2 * 24 * 60 * 60,
            7 * 24 * 60 * 60,
        ]
        for step in candidates:
            if step >= raw_step:
                return step
        return candidates[-1]

    def _iter_tick_times(self):
        if not self.start_time or not self.end_time:
            return
        step = self._nice_tick_seconds()
        start_epoch = int(self.start_time.timestamp())
        first_epoch = (start_epoch // step) * step
        if first_epoch < start_epoch:
            first_epoch += step
        tick = datetime.fromtimestamp(first_epoch, tz=self.start_time.tzinfo)
        while tick <= self.end_time:
            yield tick
            tick += timedelta(seconds=step)

    def _format_tick_label(self, value: datetime, step_seconds: int) -> str:
        if step_seconds >= 24 * 60 * 60:
            return value.strftime("%d %b\n%Y")
        if self.start_time and value.date() != self.start_time.date():
            return value.strftime("%d %b\n%H:%M")
        return value.strftime("%H:%M")

    def _draw_day_bands(self, painter: QPainter, axis_y: int):
        if not self.start_time or not self.end_time:
            return

        day = datetime(
            self.start_time.year,
            self.start_time.month,
            self.start_time.day,
            tzinfo=self.start_time.tzinfo,
        )
        if day < self.start_time:
            day += timedelta(days=1)

        while day <= self.end_time:
            x = self._time_to_x(day)
            painter.setPen(QPen(QColor("#555555"), 1))
            painter.drawLine(
                int(x), axis_y - 20, int(x), self.height() - self.bottom_pad + 2
            )
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                int(x) + 4,
                4,
                120,
                18,
                Qt.AlignLeft | Qt.AlignVCenter,
                day.strftime("%a %d %b"),
            )
            day += timedelta(days=1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101010"))

        font = QFont("Segoe UI", 9)
        painter.setFont(font)

        if not self.timeline_data or not self.start_time or not self.end_time:
            painter.setPen(QColor("#cfcfcf"))
            painter.drawText(self.rect(), Qt.AlignCenter, "No timeline data")
            return

        scroll_x = self._visible_scroll_x()
        fixed_label_x = scroll_x + 8
        fixed_label_panel = QRectF(scroll_x, 0, self.label_column_width, self.height())

        axis_y = self.top_pad - 12
        lane_start_x = self.left_pad
        lane_end_x = int(self.left_pad + self._usable_width())

        painter.setPen(QColor("#8a8a8a"))
        painter.drawLine(lane_start_x, axis_y, lane_end_x, axis_y)

        self._draw_day_bands(painter, axis_y)

        step_seconds = self._nice_tick_seconds()
        for tick_time in self._iter_tick_times() or []:
            x = self._time_to_x(tick_time)
            painter.setPen(QColor("#2a2a2a"))
            painter.drawLine(
                int(x), axis_y - 4, int(x), self.height() - self.bottom_pad + 2
            )

            painter.setPen(QColor("#d7d7d7"))
            painter.drawText(
                int(x) - 42,
                22,
                84,
                30,
                Qt.AlignCenter,
                self._format_tick_label(tick_time, step_seconds),
            )

        for row, lane in enumerate(self.timeline_data):
            y = self.top_pad + (row * self.row_height)

            painter.setPen(QColor("#2a2a2a"))
            painter.drawLine(lane_start_x, y + 22, lane_end_x, y + 22)

            for block in lane["blocks"]:
                x1 = self._time_to_x(block["start"])
                x2 = self._time_to_x(block["end"])
                if x2 < x1 + 2:
                    x2 = x1 + 2

                rect = QRectF(x1, y + 7, x2 - x1, 16)
                painter.fillRect(rect, QColor(block["color"]))
                painter.setPen(QColor("#000000"))
                painter.drawRect(rect)

                label = str(block.get("label", "")).strip()
                if label and rect.width() >= 54:
                    metrics = painter.fontMetrics()
                    text = metrics.elidedText(
                        label, Qt.ElideRight, max(1, int(rect.width()) - 8)
                    )
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(
                        rect.adjusted(4, 0, -4, 0),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        text,
                    )

        if self.current_time is not None:
            x = self._time_to_x(self.current_time)
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(
                int(x), self.top_pad - 28, int(x), self.height() - self.bottom_pad + 4
            )

        # Redraw the lane labels last as a fixed overlay tied to the scroll view.
        painter.fillRect(fixed_label_panel, QColor("#151515"))
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawLine(
            int(scroll_x + self.label_column_width),
            0,
            int(scroll_x + self.label_column_width),
            self.height(),
        )
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            int(fixed_label_x),
            22,
            self.label_column_width - 16,
            24,
            Qt.AlignLeft | Qt.AlignVCenter,
            "AMR",
        )
        for row, lane in enumerate(self.timeline_data):
            y = self.top_pad + (row * self.row_height)
            painter.setPen(QColor("#d7d7d7"))
            painter.drawText(
                int(fixed_label_x),
                y + 3,
                self.label_column_width - 16,
                24,
                Qt.AlignLeft | Qt.AlignVCenter,
                str(lane["amr_id"]),
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self._emit_seek(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed:
            self._emit_seek(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _emit_seek(self, x: float):
        new_time = self._x_to_time(x)
        if new_time is None:
            return
        parent = self.parent()
        while parent is not None and not hasattr(parent, "on_timeline_seek"):
            parent = parent.parent()
        if parent and hasattr(parent, "on_timeline_seek"):
            parent.on_timeline_seek(new_time)


class LocationInventoryPayloadDialog(QDialog):
    columns = [
        ("space", "Inventory space", 180),
        ("payload", "Current payload", 170),
        ("waste_stream", "Waste stream", 120),
        ("container_group", "Container group", 160),
        ("task_id", "Task", 100),
        ("amr_id", "AMR", 100),
        ("status", "Status", 130),
        ("timestamp", "Updated", 160),
        ("source", "Source", 150),
    ]

    def __init__(self, parent, location_name, rows, current_time):
        super().__init__(parent)
        self.setWindowTitle(f"Inventory payloads - {location_name}")
        self.resize(1420, 520)

        layout = QVBoxLayout(self)

        stamp = current_time.strftime("%Y-%m-%d %H:%M:%S") if current_time else "-"
        layout.addWidget(
            QLabel(f"Location: {location_name}\nTime: {stamp}\nSpaces: {len(rows)}")
        )

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels([h for _k, h, _w in self.columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)

        layout.addWidget(self.table, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)

        for row_data in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            tooltip = row_data.get("tooltip", "") or row_data.get("details", "")
            for c, (key, _heading, _width) in enumerate(self.columns):
                value = row_data.get(key, "-")
                item = QTableWidgetItem(str(value or "-"))
                if tooltip:
                    item.setToolTip(str(tooltip))
                self.table.setItem(r, c, item)


class TextOverlayProxy:
    """Small compatibility wrapper for text labels drawn in the overlay layer.

    Existing call sites used to receive QGraphicsSimpleTextItem and may set
    tooltip/data/rotation.  The proxy stores those values in the lightweight
    text record instead of creating a QGraphicsItem.
    """

    def __init__(self, record: dict):
        self.record = record

    def setData(self, key, value):
        self.record.setdefault("data", {})[key] = value

    def data(self, key):
        return self.record.get("data", {}).get(key)

    def setToolTip(self, value):
        self.record["tooltip"] = str(value or "")

    def toolTip(self):
        return self.record.get("tooltip", "")

    def setRotation(self, value):
        try:
            self.record["rotation"] = float(value or 0.0)
        except Exception:
            self.record["rotation"] = 0.0

    def setVisible(self, value):
        self.record["visible"] = bool(value)


class SimulationVisualizer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AMR Simulation Visualiser (PySide6)")
        self.resize(1600, 960)

        self.layout_model = LayoutModel()
        self.dxf_scenes: Dict[int, DXFScene] = {}
        self.dxf_load_thread: Optional[QThread] = None
        self.dxf_load_worker: Optional[DxfLoadWorker] = None
        self.dxf_progress_dialog: Optional[QProgressDialog] = None
        self.dxf_paths_by_floor: Dict[int, str] = {}
        self.dxf_items_by_floor: Dict[int, List[QGraphicsItem]] = {}
        self.current_dxf_floor: Optional[int] = None
        self.dxf_loading_failures: Dict[int, str] = {}
        self._dxf_text_bucket: Optional[int] = None
        self.sim_log = SimulationLog()
        self._state_cache_key = None
        self._state_cache_value = None
        self._inventory_rows_cache: Dict[Tuple[str, Optional[datetime]], List[dict]] = (
            {}
        )
        self._lift_monitor_state_cache_key = None
        self._lift_monitor_state_cache_value = None
        self._last_lift_monitor_update_wall_time = 0.0
        self._lift_monitor_update_interval_sec = 0.25

        self.current_json_path: Optional[str] = None
        self.current_dxf_path: Optional[str] = None
        self.current_csv_path: Optional[str] = None
        self.current_time: Optional[datetime] = None
        self.is_playing = False
        self.play_speed = 60.0
        self._last_play_tick_wall_time: Optional[float] = None
        self._target_playback_frame_interval_ms = 16
        self._inventory_cache_max_entries = 6000
        self._room_payload_cache_key = None
        self._brush_texture_cache: Dict[Tuple[str, int], QBrush] = {}
        self.lift_monitor_dialog: Optional[LiftMonitorDialog] = None
        self.amr_payload_monitor_dialog: Optional[AmrPayloadMonitorDialog] = None
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._tick)

        self.zoom_redraw_timer = QTimer(self)
        self.zoom_redraw_timer.setSingleShot(True)
        self.zoom_redraw_timer.timeout.connect(self.refresh_static_scene)

        self.pan_redraw_timer = QTimer(self)
        self.pan_redraw_timer.setSingleShot(True)
        self.pan_redraw_timer.timeout.connect(self._refresh_after_view_cull_change)

        self._build_ui()
        self.refresh_all()

    @staticmethod
    def _float_or_none(value):
        try:
            text = str(value).strip()
            if value is None or text == "" or text.lower() in {"nan", "none"}:
                return None
            return float(text)
        except Exception:
            return None

    @staticmethod
    def _int_or_none(value):
        try:
            text = str(value).strip()
            if value is None or text == "" or text.lower() in {"nan", "none"}:
                return None
            return int(float(text))
        except Exception:
            return None

    # def on_zoom(self):
    #     self.zoom_redraw_timer.start(20)
    #     self.refresh_static_scene()
    #     self.refresh_dynamic_scene()

    def _refresh_after_view_cull_change(self):
        """Rebuild culled map layers after the camera moves.

        Static geometry, room payloads and AMRs are culled against the visible
        world rectangle.  If a pan only repaints the existing scene, newly
        visible objects never get inserted and objects at the edge can flicker.
        Rebuild both static and dynamic layers after a short debounce so culling
        follows the current camera position.
        """
        self.refresh_static_scene()
        self.refresh_dynamic_scene()
        self.view.viewport().update()

    def on_pan(self):
        # Debounce while dragging so we do not rebuild the scene for every
        # mouse-move event, but still refresh quickly enough for culling to feel
        # attached to the camera.
        self.pan_redraw_timer.start(35)
        self.view.viewport().update()

    def on_zoom(self):
        new_bucket = self._current_dxf_text_bucket()
        if new_bucket != self._dxf_text_bucket:
            self._dxf_text_bucket = new_bucket
            floor = self.current_floor()
            if floor in self.dxf_items_by_floor:
                self.rebuild_dxf_floor_items(floor)
                if self.show_dxf_check.isChecked():
                    self.show_dxf_floor(floor)
        self.zoom_redraw_timer.start(20)
        self.refresh_dynamic_scene()
        self.view.viewport().update()

    def _current_dxf_text_bucket(self) -> int:
        scale = self.view.transform().m11()
        if scale < 6.0:
            return 0
        if scale < 12.0:
            return 1
        return 2

    def rebuild_dxf_floor_items(self, floor: int):
        old_items = self.dxf_items_by_floor.pop(floor, [])
        for item in old_items:
            self.graphics_scene.removeItem(item)
        self.ensure_dxf_floor_loaded(floor)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)

        side = QWidget()
        side.setFixedWidth(340)
        side_layout = QVBoxLayout(side)

        self.graphics_scene = QGraphicsScene(self)
        self.view = GraphicsView(self)
        self.view.setScene(self.graphics_scene)
        self.view.set_callbacks(
            zoom_callback=self.on_zoom,
            pan_callback=self.on_pan,
        )
        self.view.set_context_menu_callback(self.on_view_right_click)
        self.view.set_overlay_provider(self.draw_overlay_panels)

        self.static_items = []
        self.dynamic_items = []
        self.room_payload_items = []
        self.amr_dynamic_items = []
        self._dynamic_draw_layer = "amr"
        # Text is drawn as a separate viewport overlay layer instead of
        # QGraphicsTextItems.  The map geometry is drawn by the OpenGL-backed
        # scene pass; labels are composited afterwards for sharper text and
        # to avoid adding thousands of text items to the scene index.
        self.static_text_records = []
        self.dynamic_text_records = []
        self.room_payload_text_records = []
        self.amr_dynamic_text_records = []
        self.node_context_menu = QMenu(self)

        def add_btn(text, fn):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            side_layout.addWidget(btn)
            return btn

        add_btn("Open Layout JSON", self.open_json)
        add_btn("Open DXF", self.open_dxf)
        add_btn("Reload Current Floor DXF", self.reload_current_floor_dxf)
        add_btn("Open Simulation CSV", self.open_csv)
        add_btn("Jump to Task", self.open_task_jump_dialog)
        add_btn(
            "Tasks by Location / Department",
            self.open_tasks_by_location_department_dialog,
        )
        add_btn("Lift Monitor", self.open_lift_monitor_dialog)
        add_btn("AMR Payload Monitor", self.open_amr_payload_monitor_dialog)
        add_btn("Fit View", self.fit_view)

        side_layout.addWidget(QLabel("Floor"))
        self.floor_spin = QSpinBox()
        self.floor_spin.setRange(0, 99)
        self.floor_spin.valueChanged.connect(self.refresh_all)
        side_layout.addWidget(self.floor_spin)

        self.show_dxf_check = QCheckBox("Show DXF")
        self.show_dxf_check.setChecked(True)
        self.show_dxf_check.toggled.connect(self.refresh_static_scene)
        side_layout.addWidget(self.show_dxf_check)

        self.show_labels_check = QCheckBox("Show labels")
        self.show_labels_check.setChecked(True)
        self.show_labels_check.toggled.connect(self.refresh_all)
        side_layout.addWidget(self.show_labels_check)

        self.follow_time_check = QCheckBox("Follow slider time")
        side_layout.addWidget(self.follow_time_check)

        self.show_amr_box_check = QCheckBox("Show AMR box")
        self.show_amr_box_check.setChecked(True)
        self.show_amr_box_check.toggled.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.show_amr_box_check)

        self.show_amr_charge_state_check = QCheckBox("Show AMR charge state")
        self.show_amr_charge_state_check.setChecked(False)
        self.show_amr_charge_state_check.setToolTip(
            "Draw AMR battery percentage/charging state labels. Leave off for faster playback on large logs."
        )
        self.show_amr_charge_state_check.toggled.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.show_amr_charge_state_check)

        self.show_seeded_waste_containers_check = QCheckBox(
            "Show seeded waste containers"
        )
        self.show_seeded_waste_containers_check.setChecked(True)
        self.show_seeded_waste_containers_check.toggled.connect(self.refresh_all)
        side_layout.addWidget(self.show_seeded_waste_containers_check)

        self.show_room_payloads_check = QCheckBox("Show payloads in room spaces")
        self.show_room_payloads_check.setChecked(True)
        self.show_room_payloads_check.toggled.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.show_room_payloads_check)

        self.live_waste_fill_check = QCheckBox("Update waste fill rate during playback")
        self.live_waste_fill_check.setChecked(True)
        self.live_waste_fill_check.toggled.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.live_waste_fill_check)

        amr_size_note = QLabel("AMR size is taken from each AMR definition")
        amr_size_note.setWordWrap(True)
        side_layout.addWidget(amr_size_note)

        side_layout.addWidget(QLabel("Follow AMR"))
        self.follow_combo = QComboBox()
        self.follow_combo.currentTextChanged.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.follow_combo)

        self.follow_enabled_check = QCheckBox("Enable follow")
        self.follow_enabled_check.toggled.connect(self.refresh_dynamic_scene)
        side_layout.addWidget(self.follow_enabled_check)

        self.time_label = QLabel("No simulation loaded")
        self.time_label.setWordWrap(True)
        side_layout.addWidget(self.time_label)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self.on_slider_change)
        side_layout.addWidget(self.slider)

        side_layout.addWidget(QLabel("Playback speed (sim seconds / real second)"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1", "2", "5", "10", "30", "60", "120", "300"])
        self.speed_combo.setCurrentText("60")
        self.speed_combo.currentTextChanged.connect(self.on_speed_changed)
        side_layout.addWidget(self.speed_combo)

        side_layout.addWidget(QLabel("Loaded files"))
        self.file_label = QLabel("No files loaded")
        self.file_label.setWordWrap(True)
        side_layout.addWidget(self.file_label)

        side_layout.addWidget(QLabel("Status"))
        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        side_layout.addWidget(self.status_label)
        if getattr(self.view, "opengl_enabled", False):
            self.status_label.setText("Ready - OpenGL accelerated viewport enabled")
        elif getattr(self.view, "opengl_error", ""):
            self.status_label.setText(
                f"Warning - no Vulkan/OpenGL viewport available; raster fallback active ({self.view.opengl_error})"
            )

        # The old bottom-left event log was expensive to update on large CSVs
        # and duplicates the task/timeline views.  Keep the attribute for
        # backwards-compatible guards, but do not create or add the widget.
        self.event_box = None

        self.timeline_panel = QWidget()
        timeline_panel_layout = QVBoxLayout(self.timeline_panel)
        timeline_panel_layout.setContentsMargins(0, 0, 0, 0)
        timeline_panel_layout.setSpacing(4)

        timeline_controls = QHBoxLayout()
        timeline_controls.setContentsMargins(6, 4, 6, 0)
        self.timeline_zoom_combo = QComboBox()
        self.timeline_zoom_combo.addItems(
            [
                "Fit",
                "15 min",
                "30 min",
                "1 hour",
                "3 hours",
                "6 hours",
                "12 hours",
                "1 day",
            ]
        )
        self.timeline_zoom_combo.setCurrentText("6 hours")
        self.timeline_zoom_combo.currentTextChanged.connect(
            self.on_timeline_zoom_changed
        )

        timeline_title = QLabel("Timeline")
        timeline_title.setMinimumWidth(72)
        timeline_controls.addWidget(timeline_title)

        for text, fn in [
            ("<< Day", lambda: self.skip_timeline_days(-1)),
            ("< 6h", lambda: self.skip_timeline_hours(-6)),
            ("Today", self.jump_timeline_to_current_day_start),
            ("6h >", lambda: self.skip_timeline_hours(6)),
            ("Day >>", lambda: self.skip_timeline_days(1)),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            timeline_controls.addWidget(btn)

        timeline_controls.addSpacing(12)

        for text, fn in [
            ("|<", self.jump_start),
            ("First Move", self.jump_first_travel),
            ("-10s", lambda: self.step_seconds(-10)),
            ("Play", self.toggle_play),
            ("+10s", lambda: self.step_seconds(10)),
            (">|", self.jump_end),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            timeline_controls.addWidget(btn)
            if text == "Play":
                self.play_btn = btn

        timeline_controls.addSpacing(12)

        for text, fn in [
            ("Zoom -", lambda: self.zoom_timeline(1.35)),
            ("Zoom +", lambda: self.zoom_timeline(1 / 1.35)),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            timeline_controls.addWidget(btn)

        timeline_controls.addStretch(1)
        timeline_controls.addWidget(QLabel("Zoom"))
        timeline_controls.addWidget(self.timeline_zoom_combo)

        self.timeline_widget = AmrTimelineWidget(self)

        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(False)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.timeline_scroll.setWidget(self.timeline_widget)
        self.timeline_scroll.horizontalScrollBar().valueChanged.connect(
            self.timeline_widget.update
        )
        self.timeline_scroll.verticalScrollBar().valueChanged.connect(
            self.timeline_widget.update
        )

        timeline_panel_layout.addLayout(timeline_controls)
        timeline_panel_layout.addWidget(self.timeline_scroll, 1)

        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.view)
        self.main_splitter.addWidget(self.timeline_panel)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([760, 220])

        layout.addWidget(side)
        layout.addWidget(self.main_splitter, 1)

    def _location_by_name(self, location_name):
        for location in self.layout_model.data.get("locations", []):
            if str(location.get("name", "")).strip() == str(location_name).strip():
                return location
        return None

    def _payload_value_from_space(self, space):
        amr_id = str(space.get("amr_id", "") or "").strip()
        if amr_id:
            return f"AMR: {amr_id}"
        for key in (
            "current_payload",
            "payload",
            "payload_name",
            "stored_payload",
            "contents",
            "content",
            "item",
        ):
            value = space.get(key)
            if value not in (None, "", []):
                if isinstance(value, list):
                    return ", ".join(str(x) for x in value if str(x).strip()) or "-"
                return str(value)

        # Inventory spaces are usually defined with payload_slots rather than a
        # top-level current payload.  Treat those configured slots as initially
        # stored payloads so seeded/starting containers are visible before the
        # first CSV event has moved them.
        slots = space.get("payload_slots", []) or []
        if isinstance(slots, list):
            payloads = []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                payload = str(slot.get("payload", "") or "").strip()
                if payload:
                    payloads.append(payload)
            if payloads:
                return ", ".join(payloads)

        return "-"

    def _inventory_payload_rows_for_location(self, location_name):
        location = self._location_by_name(location_name)
        if not location:
            return []

        rows = []
        for idx, space in enumerate(
            location.get("inventory_spaces", []) or [], start=1
        ):
            payload = self._payload_value_from_space(space)
            rows.append(
                {
                    "space": str(space.get("name", "")).strip() or f"Inventory {idx}",
                    "payload": payload,
                    "task_id": space.get("task_id", "-"),
                    "amr_id": space.get("amr_id", "-"),
                    "status": "Stored" if payload != "-" else "Empty",
                    "timestamp": space.get("timestamp", "-"),
                    "source": "Layout JSON",
                }
            )

        return rows

    def show_location_inventory_payloads(self, location_name):
        point = self.layout_model.points.get(location_name, {})
        if point.get("kind") != "location":
            QMessageBox.information(
                self,
                "Inventory status",
                f"{location_name} is not a location node.",
            )
            return

        rows = self._inventory_payload_rows_for_location(location_name)
        if not rows:
            QMessageBox.information(
                self,
                f"Inventory status - {location_name}",
                f"Location: {location_name}\n\nNo inventory spaces are defined for this location.",
            )
            return

        dialog = LocationInventoryPayloadDialog(
            self,
            location_name,
            rows,
            self.current_time,
        )
        dialog.exec()

    def reload_current_floor_dxf(self):
        floor = self.current_floor()
        path = self.dxf_paths_by_floor.get(floor)

        if not path:
            QMessageBox.information(
                self, "No DXF", f"No DXF is assigned to floor {floor}."
            )
            return

        old_items = self.dxf_items_by_floor.pop(floor, [])
        for item in old_items:
            self.graphics_scene.removeItem(item)

        self.dxf_scenes.pop(floor, None)
        self.dxf_loading_failures.pop(floor, None)
        self.current_dxf_floor = None

        self._start_dxf_load_batch(
            [{"floor": floor, "filepath": path}],
            label=f"Reloading DXF for floor {floor}...",
        )

    def set_status(self, text: str):
        self.status_label.setText(text)

    def update_loaded_files(self):
        dxf_lines = []
        for floor in sorted(self.dxf_paths_by_floor):
            dxf_lines.append(f"F{floor}: {Path(self.dxf_paths_by_floor[floor]).name}")
        dxf_text = "\n".join(dxf_lines) if dxf_lines else "-"

        self.file_label.setText(
            f"JSON: {Path(self.current_json_path).name if self.current_json_path else '-'}\n"
            f"DXFs:\n{dxf_text}\n"
            f"CSV: {Path(self.current_csv_path).name if self.current_csv_path else '-'}"
        )

    def clear_all_loaded_dxf_items(self):
        self.hide_all_dxf_items()
        for floor, items in list(self.dxf_items_by_floor.items()):
            for item in items:
                self.graphics_scene.removeItem(item)
        self.dxf_scenes.clear()
        self.dxf_paths_by_floor.clear()
        self.dxf_items_by_floor.clear()
        self.dxf_loading_failures.clear()
        self.current_dxf_floor = None

    def _dxf_loader_active(self) -> bool:
        return bool(self.dxf_load_thread and self.dxf_load_thread.isRunning())

    def _start_dxf_load_batch(self, floor_dxf_files, label="Loading DXFs..."):
        floor_dxf_files = list(floor_dxf_files or [])

        if not floor_dxf_files:
            self.update_loaded_files()
            return

        if self._dxf_loader_active():
            self.set_status(
                "DXF loading is already running. Cancel it before starting another load."
            )
            return

        self.dxf_progress_dialog = QProgressDialog(
            label, "Cancel", 0, len(floor_dxf_files), self
        )
        self.dxf_progress_dialog.setWindowTitle("Loading DXFs")
        self.dxf_progress_dialog.setWindowModality(Qt.WindowModal)
        self.dxf_progress_dialog.setMinimumDuration(0)
        self.dxf_progress_dialog.setValue(0)
        self.dxf_progress_dialog.show()

        self.view.setUpdatesEnabled(False)

        self.dxf_load_thread = QThread(self)
        self.dxf_load_worker = DxfLoadWorker(floor_dxf_files)
        self.dxf_load_worker.moveToThread(self.dxf_load_thread)

        self.dxf_load_thread.started.connect(self.dxf_load_worker.run)
        self.dxf_load_worker.progress.connect(self.on_dxf_load_progress)
        self.dxf_load_worker.floor_loaded.connect(self.on_dxf_floor_loaded)
        self.dxf_load_worker.error.connect(self.on_dxf_load_error)
        self.dxf_load_worker.finished.connect(self.on_dxf_load_finished)
        self.dxf_load_worker.finished.connect(self.dxf_load_thread.quit)
        self.dxf_load_thread.finished.connect(self.dxf_load_thread.deleteLater)
        self.dxf_progress_dialog.canceled.connect(self.dxf_load_worker.cancel)

        self.dxf_load_thread.start()

    def start_loading_floor_dxfs_from_json(self):
        floor_dxf_files = self.layout_model.data.get("floor_dxf_files", [])
        self.clear_all_loaded_dxf_items()
        self.dxf_loading_failures.clear()

        if not floor_dxf_files:
            self.update_loaded_files()
            self.refresh_static_scene()
            return

        self._start_dxf_load_batch(floor_dxf_files, label="Loading DXFs...")

    def on_dxf_load_progress(self, value: int, total: int, label: str):
        if self.dxf_progress_dialog is None:
            return
        if self.dxf_progress_dialog:
            try:
                self.dxf_progress_dialog.setMaximum(total)
                self.dxf_progress_dialog.setValue(value)
                self.dxf_progress_dialog.setLabelText(label)
            except NameError as e:
                return

    def on_dxf_floor_loaded(self, floor: int, path: str, entities, bounds):
        floor = int(floor)
        self.dxf_scenes[floor] = DXFScene.from_content(path, entities, bounds)
        self.dxf_paths_by_floor[floor] = path
        self.dxf_loading_failures.pop(floor, None)

        # Match the editor principle: cache parsed DXF content for every floor,
        # but only create QGraphicsItems for the active floor on demand.
        old_items = self.dxf_items_by_floor.pop(floor, [])
        for item in old_items:
            self.graphics_scene.removeItem(item)

        self.update_loaded_files()
        if floor == self.current_floor():
            self.ensure_dxf_floor_loaded(floor)
            self.show_dxf_floor(floor)
            self.refresh_static_scene()
            self.view.viewport().update()

    def on_dxf_load_error(self, floor: int, message: str):
        self.dxf_loading_failures[int(floor)] = str(message)
        self.set_status(f"Failed DXF F{floor}: {message}")

    def on_dxf_load_finished(self):
        if self.dxf_progress_dialog:
            self.dxf_progress_dialog.setValue(self.dxf_progress_dialog.maximum())
            self.dxf_progress_dialog.close()
            self.dxf_progress_dialog = None

        self.view.setUpdatesEnabled(True)
        self.show_dxf_floor(self.current_floor())
        self.refresh_static_scene()
        self.view.viewport().update()

        if self.dxf_loading_failures:
            self.set_status(
                f"Loaded DXFs with {len(self.dxf_loading_failures)} failure(s)."
            )
        else:
            self.set_status("Loaded DXFs.")

        self.dxf_load_worker = None
        self.dxf_load_thread = None

    def current_dxf_scene(self) -> Optional[DXFScene]:
        return self.dxf_scenes.get(self.current_floor())

    def hide_all_dxf_items(self):
        for items in self.dxf_items_by_floor.values():
            for item in items:
                item.setVisible(False)

    def show_dxf_floor(self, floor: int):
        self.hide_all_dxf_items()
        for item in self.dxf_items_by_floor.get(floor, []):
            item.setVisible(self.show_dxf_check.isChecked())
        self.current_dxf_floor = floor

    def ensure_dxf_floor_loaded(self, floor: int):
        if floor in self.dxf_items_by_floor:
            return

        dxf_scene = self.dxf_scenes.get(floor)
        if not dxf_scene:
            self.dxf_items_by_floor[floor] = []
            return

        view_scale = self.view.transform().m11()
        items = dxf_scene.populate_graphics_scene(
            self.graphics_scene,
            view_scale=view_scale,
        )

        for item in items:
            item.setVisible(False)
            item.setData(0, "dxf")

        self.dxf_items_by_floor[floor] = items

    def current_floor(self) -> int:
        return int(self.floor_spin.value())

    def world_to_scene(self, x, y):
        return float(x), -float(y)

    def _visible_world_rect(
        self, margin_m: float = 2.0
    ) -> Tuple[float, float, float, float]:
        """Return current viewport bounds as world x/y min/max with a safe margin.

        Culling is based on the actual transformed viewport corners, not a stale
        scene rect.  The margin is expanded by a screen-space allowance so fast
        pans do not expose blank edges before the debounced rebuild completes.
        """
        try:
            viewport_rect = self.view.viewport().rect()
            polygon = self.view.mapToScene(viewport_rect)
            rect = polygon.boundingRect()

            scale = abs(float(self.view.transform().m11() or 1.0))
            # Keep at least 20 m of hysteresis, or roughly 160 screen pixels in
            # world units.  This prevents edge popping while moving the camera.
            safe_margin = max(float(margin_m or 0.0), 20.0, 160.0 / max(scale, 0.001))

            x_min = float(rect.left()) - safe_margin
            x_max = float(rect.right()) + safe_margin
            # Scene y is inverted relative to world y.
            y_min = -float(rect.bottom()) - safe_margin
            y_max = -float(rect.top()) + safe_margin
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            if y_min > y_max:
                y_min, y_max = y_max, y_min
            try:
                if (
                    getattr(self, "follow_enabled_check", None) is not None
                    and self.follow_enabled_check.isChecked()
                    and self.current_time is not None
                ):
                    followed_amr = self.follow_combo.currentText().strip()
                    if followed_amr:
                        amr_states, _recent = self._current_state()
                        state = amr_states.get(followed_amr) or {}
                        fx = self._float_or_none(state.get("x"))
                        fy = self._float_or_none(state.get("y"))
                        if fx is not None and fy is not None:
                            follow_margin = max(safe_margin, 35.0)
                            x_min = min(x_min, float(fx) - follow_margin)
                            x_max = max(x_max, float(fx) + follow_margin)
                            y_min = min(y_min, float(fy) - follow_margin)
                            y_max = max(y_max, float(fy) + follow_margin)
            except Exception:
                pass
            return x_min, y_min, x_max, y_max
        except Exception:
            return -1e12, -1e12, 1e12, 1e12

    @staticmethod
    def _world_bbox_intersects_rect(
        bbox: Tuple[float, float, float, float],
        rect: Tuple[float, float, float, float],
    ) -> bool:
        try:
            ax0, ay0, ax1, ay1 = [float(v) for v in bbox]
            bx0, by0, bx1, by1 = [float(v) for v in rect]
            if ax0 > ax1:
                ax0, ax1 = ax1, ax0
            if ay0 > ay1:
                ay0, ay1 = ay1, ay0
            return not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1)
        except Exception:
            return True

    @staticmethod
    def _point_in_world_rect(
        x: float, y: float, rect: Tuple[float, float, float, float]
    ) -> bool:
        x_min, y_min, x_max, y_max = rect
        return x_min <= float(x) <= x_max and y_min <= float(y) <= y_max

    @staticmethod
    def _segment_intersects_world_rect(
        a: dict, b: dict, rect: Tuple[float, float, float, float]
    ) -> bool:
        x_min, y_min, x_max, y_max = rect
        ax = float(a.get("x", 0.0) or 0.0)
        ay = float(a.get("y", 0.0) or 0.0)
        bx = float(b.get("x", 0.0) or 0.0)
        by = float(b.get("y", 0.0) or 0.0)
        return not (
            max(ax, bx) < x_min
            or min(ax, bx) > x_max
            or max(ay, by) < y_min
            or min(ay, by) > y_max
        )

    def _location_intersects_visible_world(
        self, location: dict, rect: Tuple[float, float, float, float]
    ) -> bool:
        try:
            lx = float(location.get("x", 0.0) or 0.0)
            ly = float(location.get("y", 0.0) or 0.0)
        except Exception:
            return False
        if self._point_in_world_rect(lx, ly, rect):
            return True

        bbox_points = [(lx, ly)]
        for point in location.get("bounding_box", []) or []:
            if not isinstance(point, dict):
                continue
            try:
                bbox_points.append(
                    (
                        lx + float(point.get("dx", point.get("x", 0.0)) or 0.0),
                        ly + float(point.get("dy", point.get("y", 0.0)) or 0.0),
                    )
                )
            except Exception:
                pass

        for space in location.get("inventory_spaces", []) or []:
            bbox_points.extend(self._space_points_world(location, space))

        if not bbox_points:
            return False

        xs = [p[0] for p in bbox_points]
        ys = [p[1] for p in bbox_points]
        return self._world_bbox_intersects_rect(
            (min(xs), min(ys), max(xs), max(ys)),
            rect,
        )

    def _current_state(self):
        if not self.current_time or not self.sim_log.events:
            return {}, []
        key = self.current_time
        if self._state_cache_key == key and self._state_cache_value is not None:
            return self._state_cache_value
        value = self.sim_log.state_at(self.current_time, self.layout_model)
        self._state_cache_key = key
        self._state_cache_value = value
        return value

    def _invalidate_runtime_caches(self):
        self._state_cache_key = None
        self._state_cache_value = None
        self._inventory_rows_cache.clear()
        self._lift_monitor_state_cache_key = None
        self._lift_monitor_state_cache_value = None
        self._last_play_tick_wall_time = None
        self._room_payload_cache_key = None
        if hasattr(self, "sim_log") and self.sim_log is not None:
            self.sim_log.reset_playback_cursor()

    def _current_event_index(self) -> int:
        if not self.current_time or not self.sim_log.events:
            return 0
        return self.sim_log.event_index_at(self.current_time)

    def _inventory_cache_key_for_location(self, location_name: str):
        location_name = str(location_name or "").strip()
        if self.current_time is not None and self.sim_log.events:
            event_idx = self.sim_log.location_event_index_at(location_name, self.current_time)
        else:
            event_idx = 0
        # Live waste fill values change continuously.  Bucket them so labels update
        # periodically without forcing a full inventory replay every paint frame.
        live_bucket = 0
        if (
            getattr(self, "live_waste_fill_check", None) is not None
            and self.live_waste_fill_check.isChecked()
            and self.current_time is not None
        ):
            if self.sim_log.start_time is not None:
                live_bucket = int(
                    max(0.0, (self.current_time - self.sim_log.start_time).total_seconds())
                    // 30.0
                )
            else:
                live_bucket = int(self.current_time.timestamp() // 30.0)
        return (location_name, event_idx, live_bucket)

    def _trim_inventory_rows_cache(self):
        limit = int(getattr(self, "_inventory_cache_max_entries", 6000) or 6000)
        if limit <= 0 or len(self._inventory_rows_cache) <= limit:
            return
        remove_count = max(1, len(self._inventory_rows_cache) - limit)
        for key in list(self._inventory_rows_cache.keys())[:remove_count]:
            self._inventory_rows_cache.pop(key, None)

    def clear_items(self, items):
        for item in items:
            self.graphics_scene.removeItem(item)
        items.clear()

    def _active_dynamic_items(self):
        if getattr(self, "_dynamic_draw_layer", "amr") == "room":
            return self.room_payload_items
        return self.amr_dynamic_items

    def _active_dynamic_text_records(self):
        if getattr(self, "_dynamic_draw_layer", "amr") == "room":
            return self.room_payload_text_records
        return self.amr_dynamic_text_records

    def _visible_world_cache_bucket(self) -> Tuple[int, int, int, int, int]:
        rect = self._visible_world_rect(margin_m=12.0)
        scale_bucket = int(max(1.0, float(self.view.transform().m11() or 1.0)) * 10)
        return tuple(int(math.floor(value / 10.0)) for value in rect) + (scale_bucket,)

    def _followed_amr_cache_bucket(self) -> Tuple[str, int, int, int]:
        if not self.follow_enabled_check.isChecked() or not self.current_time:
            return ("", 0, 0, self.current_floor())
        followed_amr = self.follow_combo.currentText().strip()
        if not followed_amr:
            return ("", 0, 0, self.current_floor())
        amr_states, _recent = self._current_state()
        state = amr_states.get(followed_amr) or {}
        x = self._float_or_none(state.get("x"))
        y = self._float_or_none(state.get("y"))
        if x is None or y is None:
            return (followed_amr, 0, 0, self.current_floor())
        return (followed_amr, int(math.floor(float(x) / 10.0)), int(math.floor(float(y) / 10.0)), int(state.get("floor", self.current_floor()) or self.current_floor()))

    def _room_payload_scene_cache_key(self):
        event_idx = self._current_event_index()
        live_bucket = 0
        if self.current_time is not None and self.sim_log.start_time is not None:
            live_bucket = int(
                max(0.0, (self.current_time - self.sim_log.start_time).total_seconds())
                // 30.0
            )
        return (
            self.current_floor(),
            event_idx,
            live_bucket,
            self.show_room_payloads_check.isChecked(),
            self.show_labels_check.isChecked(),
            self._visible_world_cache_bucket(),
            self._followed_amr_cache_bucket(),
        )

    def refresh_all(self):
        self.refresh_static_scene()
        self.refresh_dynamic_scene()
        self.refresh_timeline()

    def refresh_static_scene(self):
        self.clear_items(self.static_items)
        if hasattr(self, "static_text_records"):
            self.static_text_records.clear()
        self._room_payload_cache_key = None
        floor = self.current_floor()

        if self.show_dxf_check.isChecked():
            self.ensure_dxf_floor_loaded(floor)
            self.show_dxf_floor(floor)
        else:
            self.hide_all_dxf_items()

        self.draw_layout_qt(floor)
        self.view.viewport().update()

    def refresh_dynamic_scene(self):
        self.clear_items(self.amr_dynamic_items)
        if hasattr(self, "amr_dynamic_text_records"):
            self.amr_dynamic_text_records.clear()
        # Do not clear inventory-row caches every frame.  The cache key includes
        # the current event index and a small live-fill time bucket, so normal
        # playback can reuse room payload reconstruction while AMRs move between
        # CSV inventory events.
        payload_cache_key = self._room_payload_scene_cache_key()
        if payload_cache_key != self._room_payload_cache_key:
            self.clear_items(self.room_payload_items)
            if hasattr(self, "room_payload_text_records"):
                self.room_payload_text_records.clear()
            self._dynamic_draw_layer = "room"
            self.draw_room_payloads_qt(self.current_floor())
            self._dynamic_draw_layer = "amr"
            self._room_payload_cache_key = payload_cache_key
        else:
            self._dynamic_draw_layer = "amr"
        self.draw_dynamic_state_qt(self.current_floor())
        self.dynamic_items = self.room_payload_items + self.amr_dynamic_items
        self.dynamic_text_records = (
            self.room_payload_text_records + self.amr_dynamic_text_records
        )
        self.update_follow_view()
        self.update_lift_monitor_dialog()
        self.update_amr_payload_monitor_dialog()
        self.view.viewport().update()

    def draw_line_item(self, x1, y1, x2, y2, color="#858585", width=0.0, dynamic=False):
        item = QGraphicsLineItem(x1, y1, x2, y2)
        pen = QPen(QColor(color))
        pen.setWidthF(width)
        item.setPen(pen)
        self.graphics_scene.addItem(item)
        (self._active_dynamic_items() if dynamic else self.static_items).append(item)
        return item

    def get_text_pixel_size(self) -> int:
        # Retained for older callers, but text is no longer sized from the
        # current zoom level.  Labels are drawn at a fixed scene/map size, so
        # they naturally appear larger when zooming in and smaller when
        # zooming out.
        return 9

    def _fixed_scene_text_height(self, dynamic: bool = False) -> float:
        # Text height in scene/world units.  This is intentionally independent
        # of the view transform.  QGraphicsView then scales the text naturally
        # with the rest of the drawing.
        return 0.38 if dynamic else 0.45

    def _texture_brush(self, color, alpha: int = 180) -> QBrush:
        """Return a tiny raster texture brush for repeated moving-object fills."""
        qcolor = QColor(color)
        qcolor.setAlpha(max(0, min(255, int(alpha))))
        key = (qcolor.name(QColor.HexArgb), int(alpha))
        cached = self._brush_texture_cache.get(key)
        if cached is not None:
            return cached

        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(0, 0, 16, 16, qcolor)
        highlight = QColor("#ffffff")
        highlight.setAlpha(28)
        painter.setPen(QPen(highlight, 1))
        painter.drawLine(0, 15, 15, 0)
        painter.drawLine(-8, 15, 7, 0)
        painter.end()
        brush = QBrush(pixmap)
        self._brush_texture_cache[key] = brush
        return brush

    def draw_text_item(
        self,
        x,
        y,
        text,
        color="white",
        dynamic=False,
        ignore_transform=False,
        pixel_size: Optional[float] = None,
    ):
        """Queue text for the overlay label layer.

        Geometry is rendered by the QGraphicsScene using an OpenGL-backed
        viewport.  Text is deliberately not added to the scene as a
        QGraphicsItem; it is painted afterwards in viewport coordinates.  The
        stored height is in scene/world units, so zooming the camera still makes
        labels appear larger when closer and smaller when zoomed out, without
        the blur/spacing problems caused by transformed text items.
        """
        text = str(text or "")
        scene_height = self._fixed_scene_text_height(bool(dynamic))
        if pixel_size is not None:
            # Older callers pass a relative pixel size.  Convert it into a
            # smaller scene-height hint so payload-space labels can be made
            # deliberately smaller without reverting to fixed screen text.
            try:
                scene_height *= max(0.35, min(1.25, float(pixel_size) / 9.0))
            except Exception:
                pass
        record = {
            "kind": "point_text",
            "x": float(x),
            "y": float(y),
            "text": text,
            "color": str(color or "white"),
            "scene_height": scene_height,
            "dynamic": bool(dynamic),
            "rotation": 0.0,
            "visible": True,
            "tooltip": "",
            "data": {},
        }
        if dynamic:
            self._active_dynamic_text_records().append(record)
        else:
            self.static_text_records.append(record)
        return TextOverlayProxy(record)

    def draw_fitted_text_box_item(
        self,
        x,
        y,
        width_scene: float,
        height_scene: float,
        text,
        color="white",
        dynamic=True,
        rotation_deg: float = 0.0,
        max_lines: int = 4,
        min_pixel_size: int = 3,
        max_pixel_size: Optional[int] = None,
        scene_height: Optional[float] = None,
    ):
        """Queue a label that is forced to fit inside a scene-space box.

        This is used for payload boxes.  The font size is stored in scene/world
        units, exactly like normal overlay labels, so camera zoom naturally makes
        the text larger when zooming in and smaller when zooming out.  The draw
        pass only shrinks below that scene height when needed to keep the text
        inside the payload footprint.
        """
        if scene_height is None:
            # Smaller than normal room labels because payload boxes are compact.
            scene_height = self._fixed_scene_text_height(bool(dynamic)) * 0.42
        record = {
            "kind": "box_text",
            "x": float(x),
            "y": float(y),
            "box_width_scene": max(0.01, float(width_scene or 0.01)),
            "box_height_scene": max(0.01, float(height_scene or 0.01)),
            "text": str(text or ""),
            "color": str(color or "white"),
            "dynamic": bool(dynamic),
            "rotation": float(rotation_deg or 0.0),
            "visible": True,
            "tooltip": "",
            "data": {},
            "max_lines": max(1, int(max_lines or 1)),
            "min_pixel_size": max(1, int(min_pixel_size or 1)),
            "max_pixel_size": max_pixel_size if max_pixel_size is not None else None,
            "scene_height": max(0.02, float(scene_height or 0.02)),
        }
        if dynamic:
            self._active_dynamic_text_records().append(record)
        else:
            self.static_text_records.append(record)
        return TextOverlayProxy(record)

    def _wrap_text_to_width(
        self, painter: QPainter, text: str, max_width: float, max_lines: int
    ) -> List[str]:
        """Wrap label text to a pixel width using the current painter font."""
        text = str(text or "")
        max_width = max(1.0, float(max_width or 1.0))
        max_lines = max(1, int(max_lines or 1))
        metrics = painter.fontMetrics()
        lines = []

        for raw_line in text.splitlines() or [text]:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            words = raw_line.split()
            if not words:
                continue
            line = words[0]
            for word in words[1:]:
                candidate = f"{line} {word}"
                if metrics.horizontalAdvance(candidate) <= max_width:
                    line = candidate
                else:
                    lines.append(line)
                    line = word
                    if len(lines) >= max_lines:
                        break
            if len(lines) >= max_lines:
                break
            lines.append(line)
            if len(lines) >= max_lines:
                break

        if not lines:
            return []

        # Elide the last line if even the wrapped text is still too long or if
        # earlier content was truncated due to max_lines.
        if len(lines) >= max_lines:
            lines[-1] = metrics.elidedText(lines[-1], Qt.ElideRight, int(max_width))
        else:
            lines = [
                metrics.elidedText(line, Qt.ElideRight, int(max_width))
                for line in lines
            ]
        return lines[:max_lines]

    def _draw_fitted_box_text_record(
        self,
        painter: QPainter,
        record: dict,
        transform_scale: float,
        viewport_rect: QRect,
    ):
        text = str(record.get("text", "") or "").strip()
        if not text:
            return

        scene_pos = QPointF(float(record.get("x", 0.0)), float(record.get("y", 0.0)))
        view_pos = self.view.mapFromScene(scene_pos)
        if (
            view_pos.x() < -250
            or view_pos.y() < -120
            or view_pos.x() > viewport_rect.width() + 250
            or view_pos.y() > viewport_rect.height() + 120
        ):
            return

        box_w = max(
            1.0, float(record.get("box_width_scene", 0.01) or 0.01) * transform_scale
        )
        box_h = max(
            1.0, float(record.get("box_height_scene", 0.01) or 0.01) * transform_scale
        )
        pad = max(1.0, min(box_w, box_h) * 0.08)
        usable_w = max(1.0, box_w - (pad * 2.0))
        usable_h = max(1.0, box_h - (pad * 2.0))
        max_lines = max(1, int(record.get("max_lines", 4) or 4))
        min_px = max(1, int(record.get("min_pixel_size", 3) or 3))

        scene_height = float(record.get("scene_height", 0.16) or 0.16)
        natural_px = max(min_px, int(round(scene_height * transform_scale)))
        max_px_value = record.get("max_pixel_size", None)
        if max_px_value is not None:
            try:
                natural_px = min(natural_px, max(min_px, int(max_px_value)))
            except Exception:
                pass

        # Start from the same camera-scaled size as normal labels.  Only shrink
        # when the current zoom/font would overflow the payload box.
        candidate_px = max(min_px, natural_px)
        best_lines = []
        best_px = min_px

        font = QFont("Arial")
        font.setStyleStrategy(QFont.PreferAntialias)
        font.setHintingPreference(QFont.PreferFullHinting)
        for px in range(candidate_px, min_px - 1, -1):
            font.setPixelSize(px)
            painter.setFont(font)
            lines = self._wrap_text_to_width(painter, text, usable_w, max_lines)
            metrics = painter.fontMetrics()
            line_height = max(1, metrics.lineSpacing())
            widest = max((metrics.horizontalAdvance(line) for line in lines), default=0)
            total_h = line_height * len(lines)
            if lines and widest <= usable_w and total_h <= usable_h:
                best_lines = lines
                best_px = px
                break
            if lines:
                best_lines = lines
                best_px = px

        if not best_lines:
            return

        font.setPixelSize(best_px)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        line_height = max(1, metrics.lineSpacing())
        total_h = line_height * len(best_lines)

        painter.save()
        painter.translate(view_pos)
        # Payload/world rotation appears mirrored in the scene because world_to_scene
        # flips Y.  Negate it so text follows the visible payload footprint.
        rotation = -float(record.get("rotation", 0.0) or 0.0)
        if rotation:
            painter.rotate(rotation)
        painter.setPen(QColor(str(record.get("color", "white") or "white")))

        y0 = -total_h / 2.0 + metrics.ascent()
        for line_no, line in enumerate(best_lines):
            y = y0 + (line_no * line_height)
            painter.drawText(
                QRectF(-usable_w / 2.0, y - metrics.ascent(), usable_w, line_height),
                Qt.AlignCenter,
                line,
            )
        painter.restore()

    def _draw_text_overlay_records(self, painter: QPainter, viewport_rect: QRect):
        """Draw queued scene-space text as a separate viewport overlay layer."""
        if not hasattr(self, "view"):
            return

        records = []
        records.extend(getattr(self, "static_text_records", []) or [])
        records.extend(getattr(self, "dynamic_text_records", []) or [])
        if not records:
            return

        transform_scale = max(0.0001, float(self.view.transform().m11() or 1.0))
        # Convert the fixed scene text height to viewport pixels.  This preserves
        # the correct camera behaviour: zoom in -> text appears larger; zoom out
        # -> text appears smaller.  A small lower clamp prevents zero-size fonts.
        base_scene_height = self._fixed_scene_text_height(False)
        base_px = max(3, min(220, int(round(base_scene_height * transform_scale))))

        font = QFont("Arial")
        font.setPixelSize(base_px)
        font.setStyleStrategy(QFont.PreferAntialias)
        font.setHintingPreference(QFont.PreferFullHinting)
        painter.save()
        painter.setFont(font)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        for record in records:
            if not record.get("visible", True):
                continue
            text = str(record.get("text", "") or "")
            if not text:
                continue

            if record.get("kind") == "box_text":
                self._draw_fitted_box_text_record(
                    painter, record, transform_scale, viewport_rect
                )
                continue

            scene_pos = QPointF(
                float(record.get("x", 0.0)), float(record.get("y", 0.0))
            )
            view_pos = self.view.mapFromScene(scene_pos)
            # Coarse viewport culling.  Text is allowed a margin so labels do not
            # pop at the edge of the screen when partially visible.
            if (
                view_pos.x() < -250
                or view_pos.y() < -80
                or view_pos.x() > viewport_rect.width() + 250
                or view_pos.y() > viewport_rect.height() + 80
            ):
                continue

            scene_height = float(
                record.get("scene_height", base_scene_height) or base_scene_height
            )
            px = max(3, min(220, int(round(scene_height * transform_scale))))
            if font.pixelSize() != px:
                font.setPixelSize(px)
                painter.setFont(font)

            painter.save()
            painter.translate(view_pos)
            rotation = float(record.get("rotation", 0.0) or 0.0)
            if rotation:
                painter.rotate(rotation)
            painter.setPen(QColor(str(record.get("color", "white") or "white")))
            painter.drawText(QPointF(0, 0), text)
            painter.restore()

        painter.restore()

    def draw_dxf_scene_qt(self):
        visible_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        visible_world = (
            visible_rect.left(),
            -visible_rect.bottom(),
            visible_rect.right(),
            -visible_rect.top(),
        )

        for entity in self.dxf_scene.entities:
            if not self.dxf_scene._bbox_intersects(entity.get("bbox"), visible_world):
                continue

            etype = entity["type"]
            if etype == "LINE":
                x1, y1 = self.world_to_scene(*entity["start"])
                x2, y2 = self.world_to_scene(*entity["end"])
                self.draw_line_item(x1, y1, x2, y2, "#858585")
            elif etype == "POLYLINE":
                pts = [QPointF(*self.world_to_scene(x, y)) for x, y in entity["points"]]
                for i in range(len(pts) - 1):
                    self.draw_line_item(
                        pts[i].x(),
                        pts[i].y(),
                        pts[i + 1].x(),
                        pts[i + 1].y(),
                        "#858585",
                    )
                if entity.get("closed") and len(pts) > 2:
                    self.draw_line_item(
                        pts[-1].x(), pts[-1].y(), pts[0].x(), pts[0].y(), "#858585"
                    )
            elif etype == "CIRCLE":
                cx, cy = self.world_to_scene(*entity["center"])
                r = float(entity["radius"])
                item = QGraphicsEllipseItem(cx - r, cy - r, r * 2, r * 2)
                item.setPen(QPen(QColor("#858585"), 0.0))
                self.graphics_scene.addItem(item)
                self.static_items.append(item)
            elif etype == "ARC":
                cx, cy = self.world_to_scene(*entity["center"])
                r = float(entity["radius"])

                start_angle = float(entity.get("start_angle", 0.0))
                end_angle = float(entity.get("end_angle", 0.0))

                span_angle = end_angle - start_angle
                if span_angle <= 0:
                    span_angle += 360.0

                rect = QRectF(cx - r, cy - r, r * 2, r * 2)

                path = QPainterPath()
                # Qt arc angles are counter-clockwise in degrees, but your Y axis is flipped
                # by world_to_scene(), so negate the angles for the correct visual direction.
                path.arcMoveTo(rect, -start_angle)
                path.arcTo(rect, -start_angle, -span_angle)

                item = QGraphicsPathItem(path)
                pen = QPen(QColor("#2e2e2e"))
                pen.setWidthF(0.0)
                item.setPen(pen)
                item.setBrush(Qt.NoBrush)
                self.graphics_scene.addItem(item)
                self.static_items.append(item)
            elif etype == "TEXT":
                text = (entity.get("text") or "").strip()
                if not text:
                    continue
                if self.view.transform().m11() < 0.3:
                    continue

                # Skip absurdly large DXF text objects that can stall the scene.
                text_height = float(entity.get("height") or 0.0)
                if text_height > 20.0:
                    continue

                x, y = self.world_to_scene(*entity["insert"])
                item = self.draw_text_item(
                    x,
                    y,
                    text,
                    "#858585",
                    ignore_transform=True,
                )
                item.setRotation(-float(entity.get("rotation", 0.0)))

    def draw_layout_qt(self, floor: int):
        visible_world = self._visible_world_rect(margin_m=8.0)

        for edge in self.layout_model.edges_for_floor(floor):
            a = self.layout_model.points.get(edge["from"])
            b = self.layout_model.points.get(edge["to"])
            if not a or not b:
                continue
            if not self._segment_intersects_world_rect(a, b, visible_world):
                continue
            ax, ay = self.world_to_scene(a["x"], a["y"])
            bx, by = self.world_to_scene(b["x"], b["y"])
            self.draw_line_item(ax, ay, bx, by, "#5f8dd3", 0.0)

        for name, point in self.layout_model.points_for_floor(floor).items():
            if not self._point_in_world_rect(
                float(point.get("x", 0.0) or 0.0),
                float(point.get("y", 0.0) or 0.0),
                visible_world,
            ):
                continue
            x, y = self.world_to_scene(point["x"], point["y"])
            kind = point.get("kind")
            if kind == "location":
                item = QGraphicsEllipseItem(x - 0.5, y - 0.5, 1.0, 1.0)
                item.setBrush(QBrush(QColor("#18c37e")))
                item.setPen(QPen(Qt.NoPen))
                color = "#9bf0cd"
            elif kind == "corridor_node":
                item = QGraphicsRectItem(x - 0.4, y - 0.4, 0.8, 0.8)
                item.setBrush(QBrush(QColor("#f2c94c")))
                item.setPen(QPen(Qt.NoPen))
                color = "#ffe8a3"
            else:
                poly = QPolygonF(
                    [
                        QPointF(x, y - 0.6),
                        QPointF(x + 0.6, y),
                        QPointF(x, y + 0.6),
                        QPointF(x - 0.6, y),
                    ]
                )
                item = QGraphicsPolygonItem(poly)
                item.setBrush(QBrush(QColor("#ff7b72")))
                item.setPen(QPen(Qt.NoPen))
                color = "#ffb3ae"

            item.setData(0, "layout_node")
            item.setData(1, name)
            self.graphics_scene.addItem(item)
            self.static_items.append(item)

            if self.show_labels_check.isChecked():
                label_item = self.draw_text_item(
                    x + 0.8, y - 0.8, name, color, ignore_transform=True
                )
                if label_item is not None:
                    label_item.setData(0, "layout_node_label")
                    label_item.setData(1, name)

    def _payload_lookup(self) -> Dict[str, dict]:
        lookup = {}
        for payload in self.layout_model.data.get("payloads", []) or []:
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name", "") or "").strip()
            if name:
                lookup[name] = payload
        return lookup

    def _payload_dimensions_for_name(self, payload_name: str) -> Tuple[float, float]:
        payload_name = str(payload_name or "").strip()
        payload = self._payload_lookup().get(payload_name, {})
        try:
            length = float(
                payload.get(
                    "length_m", payload.get("length", payload.get("depth_m", 0.9))
                )
                or 0.9
            )
        except Exception:
            length = 0.9
        try:
            width = float(payload.get("width_m", payload.get("width", 0.65)) or 0.65)
        except Exception:
            width = 0.65
        return max(0.15, length), max(0.15, width)

    def _amr_dimensions_for_name(self, amr_name: str) -> Tuple[float, float]:
        amr_name = str(amr_name or "").strip()
        base_name = amr_name.rsplit("-", 1)[0] if "-" in amr_name else amr_name
        for amr in self.layout_model.data.get("amrs", []) or []:
            if not isinstance(amr, dict):
                continue
            amr_id = str(amr.get("id", "") or "").strip()
            if amr_id not in {amr_name, base_name}:
                continue
            try:
                length = float(amr.get("length_m", 0.8) or 0.8)
            except Exception:
                length = 0.8
            try:
                width = float(amr.get("width_m", 0.6) or 0.6)
            except Exception:
                width = 0.6
            return max(0.15, length), max(0.15, width)
        return 0.8, 0.6

    def _payload_full_details_for_name(self, payload_name: str) -> dict:
        payload_name = str(payload_name or "").strip()
        payload = self._payload_lookup().get(payload_name, {}) or {}
        if str(payload_name or "").startswith("AMR: "):
            length, width = self._amr_dimensions_for_name(str(payload_name).split(":", 1)[1].strip())
        else:
            length, width = self._payload_dimensions_for_name(payload_name)

        def num(*keys, default=0.0):
            for key in keys:
                try:
                    value = payload.get(key, None)
                    if value not in (None, ""):
                        return float(value)
                except Exception:
                    pass
            return float(default)

        height = num("height_m", "height", default=0.0)
        weight = num("weight_kg", "payload_weight_kg", "mass_kg", default=0.0)
        return {
            "name": payload_name,
            "length_m": length,
            "width_m": width,
            "height_m": height,
            "weight_kg": weight,
            "raw": payload,
        }

    def _waste_stream_capacity_for_name(self, stream_name: str) -> float:
        stream_name = str(stream_name or "").strip()
        if not stream_name:
            return 0.0
        stream_def = self._waste_stream_lookup().get(stream_name, {}) or {}
        for key in ("container_capacity_m3", "capacity_m3", "volume_m3"):
            try:
                value = stream_def.get(key, None)
                if value not in (None, ""):
                    return max(0.0, float(value))
            except Exception:
                pass
        return 0.0

    def _waste_stream_threshold_for_name(self, stream_name: str) -> float:
        """Return the visual collection threshold for a stream in m³.

        The physical capacity is still shown in labels, but a threshold waste
        container should not visually fill beyond the configured collection
        threshold, for example 80% of a 0.77 m³ bin.
        """
        stream_name = str(stream_name or "").strip()
        if not stream_name:
            return 0.0
        stream_def = self._waste_stream_lookup().get(stream_name, {}) or {}
        explicit = self._row_float(stream_def, "threshold_volume_m3", default=0.0)
        if explicit > 0.0:
            return explicit
        capacity = self._waste_stream_capacity_for_name(stream_name)
        fraction = self._row_float(stream_def, "full_threshold_fraction", default=0.8)
        if capacity > 0.0 and fraction > 0.0:
            return max(0.0, capacity * fraction)
        return 0.0

    def _row_float(self, row: dict, *keys, default: float = 0.0) -> float:
        for key in keys:
            try:
                value = row.get(key, None)
                if value not in (None, ""):
                    return float(value)
            except Exception:
                pass
        return float(default)

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

    def _day_key_for_datetime(self, value: datetime) -> str:
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][value.weekday()]

    def _department_by_id_or_name(
        self, department_id: str = "", department_name: str = ""
    ) -> Optional[dict]:
        department_id = str(department_id or "").strip()
        department_name = str(department_name or "").strip()
        for department in self.layout_model.data.get("departments", []) or []:
            if not isinstance(department, dict):
                continue
            candidate_id = (
                str(department.get("id", "") or "").strip()
                or str(department.get("name", "") or "").strip()
            )
            candidate_name = str(department.get("name", "") or "").strip()
            if department_id and department_id in {candidate_id, candidate_name}:
                return department
            if department_name and department_name in {candidate_id, candidate_name}:
                return department
        return None

    def _department_operating_start_minutes(self, department: dict) -> int:
        explicit = self._parse_hhmm_to_minutes(
            department.get("operating_start_time"), None
        )
        return int(explicit if explicit is not None else 0)

    def _department_operating_end_minutes(self, department: dict) -> int:
        explicit = self._parse_hhmm_to_minutes(
            department.get("operating_end_time"), None
        )
        if explicit is not None:
            return int(explicit)
        start = self._department_operating_start_minutes(department)
        try:
            hours = float(department.get("hours_operated_per_day", 24.0) or 24.0)
        except Exception:
            hours = 24.0
        if hours >= 24.0:
            return start + (24 * 60)
        return start + int(round(max(0.0, hours) * 60.0))

    def _department_operating_periods_for_date(
        self, department: Optional[dict], day: datetime
    ) -> List[Tuple[datetime, datetime]]:
        if not department:
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            return [(day_start, day_start + timedelta(days=1))]

        if not bool(department.get("enabled", True)):
            return []

        active_days = department.get("days_active", []) or []
        if active_days:
            allowed = {
                str(x or "").strip().lower()
                for x in active_days
                if str(x or "").strip()
            }
            if self._day_key_for_datetime(day) not in allowed:
                return []

        start_min = self._department_operating_start_minutes(department)
        end_min = self._department_operating_end_minutes(department)
        if end_min <= start_min:
            end_min += 24 * 60

        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = day_start + timedelta(minutes=start_min)
        end_dt = day_start + timedelta(minutes=end_min)
        if end_dt <= start_dt:
            return []
        return [(start_dt, end_dt)]

    def _department_active_seconds_between(
        self, department: Optional[dict], start_dt: datetime, end_dt: datetime
    ) -> float:
        if end_dt <= start_dt:
            return 0.0
        if not department:
            return max(0.0, (end_dt - start_dt).total_seconds())

        total = 0.0
        cursor_day = (start_dt - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        final_day = end_dt.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        while cursor_day <= final_day:
            for period_start, period_end in self._department_operating_periods_for_date(
                department, cursor_day
            ):
                overlap_start = max(start_dt, period_start)
                overlap_end = min(end_dt, period_end)
                if overlap_end > overlap_start:
                    total += (overlap_end - overlap_start).total_seconds()
            cursor_day += timedelta(days=1)
        return total

    def _normalise_live_waste_contributors(self, value) -> List[dict]:
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except Exception:
            pass
        return []

    def _live_waste_increment_m3(
        self,
        row: dict,
        start_time: datetime,
        at_time: datetime,
        fallback_daily_rate: float,
    ) -> float:
        contributors = self._normalise_live_waste_contributors(
            row.get("live_waste_contributors", [])
        )
        if contributors:
            total = 0.0
            for contributor in contributors:
                rate = self._row_float(
                    contributor,
                    "daily_rate_m3_per_day",
                    "live_waste_volume_m3_per_day",
                    "daily_waste_volume_m3",
                    default=0.0,
                )
                if rate <= 0.0:
                    continue
                department = (
                    self._department_by_id_or_name(
                        contributor.get("department_id", ""),
                        contributor.get("department_name", ""),
                    )
                    or contributor
                )
                active_seconds = self._department_active_seconds_between(
                    department, start_time, at_time
                )
                total += max(0.0, rate) * (active_seconds / 86400.0)
            return max(0.0, total)

        department = self._department_by_id_or_name(
            row.get("department_id", ""), row.get("department_name", "")
        )
        active_seconds = self._department_active_seconds_between(
            department, start_time, at_time
        )
        return max(0.0, fallback_daily_rate) * (active_seconds / 86400.0)

    def _waste_stream_daily_volume_m3(self, stream_cfg: dict) -> float:
        """Return the configured waste accumulation rate in m³/day."""
        if not isinstance(stream_cfg, dict):
            return 0.0

        base_daily = self._row_float(stream_cfg, "base_daily_volume_m3", default=0.0)
        frequency = self._row_float(stream_cfg, "frequency_per_day", default=0.0)
        volume_per_event = self._row_float(
            stream_cfg, "volume_per_event_m3", default=0.0
        )

        rate = max(0.0, base_daily)
        if frequency > 0.0 and volume_per_event > 0.0:
            rate += max(0.0, frequency * volume_per_event)

        # Scheduled streams may use volume_per_event without a frequency.  In
        # that case treat each configured scheduled time as one fill event.
        scheduled_times = (
            stream_cfg.get("scheduled_times", [])
            or stream_cfg.get("schedule_times", [])
            or []
        )
        if (
            isinstance(scheduled_times, list)
            and scheduled_times
            and volume_per_event > 0.0
            and frequency <= 0.0
        ):
            rate += (
                len([x for x in scheduled_times if str(x).strip()]) * volume_per_event
            )

        return max(0.0, rate)

    def _parse_row_timestamp(self, value) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        if text.lower() in {"simulation start", "start"}:
            return self.sim_log.start_time or self.layout_model.task_start_time
        return SimulationLog._parse_datetime(text) or LayoutModel._parse_datetime(text)

    def _live_waste_fill_enabled(self) -> bool:
        return bool(
            not hasattr(self, "live_waste_fill_check")
            or self.live_waste_fill_check.isChecked()
        )

    def _apply_live_waste_fill(
        self, row: dict, at_time: Optional[datetime] = None
    ) -> dict:
        """Continuously update waste container fill while it is present in a location."""
        row = dict(row or {})
        if not self._live_waste_fill_enabled():
            return row

        if at_time is None:
            at_time = self.current_time
        if at_time is None:
            return row

        payload = str(row.get("payload", "") or "").strip()
        if not payload or payload == "-":
            return row

        stream_name = str(row.get("waste_stream", "") or "").strip()
        daily_rate = self._row_float(
            row,
            "live_waste_volume_m3_per_day",
            "daily_waste_volume_m3",
            "fill_rate_m3_per_day",
            default=0.0,
        )
        capacity = self._row_float(
            row, "container_capacity_m3", "capacity_m3", default=0.0
        )
        if capacity <= 0.0 and stream_name:
            capacity = self._waste_stream_capacity_for_name(stream_name)
        threshold_m3 = self._row_float(
            row, "container_threshold_m3", "threshold_volume_m3", default=0.0
        )
        if threshold_m3 <= 0.0 and stream_name:
            threshold_m3 = self._waste_stream_threshold_for_name(stream_name)

        if not stream_name and daily_rate <= 0.0 and capacity <= 0.0:
            return row

        start_time = (
            self._parse_row_timestamp(row.get("fill_start_time"))
            or self._parse_row_timestamp(row.get("timestamp"))
            or self.sim_log.start_time
            or self.layout_model.task_start_time
        )
        if start_time is None or at_time <= start_time:
            return row

        # Do not use CSV waste_volume_m3 as the starting fill value.  In the
        # simulator CSV that field means "volume collected by this task", not
        # "current volume inside the returned/seeded bin".
        base_volume = self._row_float(
            row,
            "fill_start_volume_m3",
            "initial_waste_volume_m3",
            "initial_volume_m3",
            "current_volume_m3",
            "volume_filled_m3",
            "filled_volume_m3",
            default=0.0,
        )

        # Match the simulator waste generator: waste only accumulates during
        # the department operating windows.  Shared bins can have multiple
        # contributing departments, each with its own operating hours.
        volume = base_volume + self._live_waste_increment_m3(
            row, start_time, at_time, daily_rate
        )
        # The threshold is a collection trigger, not a physical limit.
        # Keep the live fill increasing beyond the collection threshold and
        # only cap at the actual container capacity when a capacity is known.
        if capacity > 0.0:
            volume = min(capacity, volume)
        row["waste_volume_m3"] = max(0.0, volume)
        row["container_capacity_m3"] = capacity
        if threshold_m3 > 0.0:
            row["container_threshold_m3"] = threshold_m3
            if capacity > 0.0:
                row["threshold_display"] = (
                    f"Trigger {threshold_m3:.3f} m³ ({(threshold_m3 / capacity) * 100.0:.0f}%)"
                )
            else:
                row["threshold_display"] = f"Trigger {threshold_m3:.3f} m³"
        if daily_rate > 0.0:
            row["fill_rate_display"] = f"{daily_rate:.3f} m³/day"
        return row

    def _enrich_payload_row_details(self, row: dict) -> dict:
        row = self._apply_live_waste_fill(dict(row or {}))
        payload_name = str(row.get("payload", "") or "").strip()
        if not payload_name or payload_name == "-":
            row.setdefault("details", "-")
            row.setdefault("waste_volume_display", "-")
            row.setdefault("fill_percent_display", "-")
            row.setdefault(
                "payload_instance_id", row.get("payload_instance_id", "-") or "-"
            )
            row.setdefault("tooltip", "")
            return row

        details = self._payload_full_details_for_name(payload_name)
        dims = f"{details['length_m']:.2f} x {details['width_m']:.2f}"
        if details.get("height_m", 0.0) > 0:
            dims += f" x {details['height_m']:.2f} m"
        else:
            dims += " m"

        detail_parts = [f"Size {dims}"]
        if details.get("weight_kg", 0.0) > 0:
            detail_parts.append(f"Weight {details['weight_kg']:.1f} kg")
        row["details"] = " | ".join(detail_parts)

        stream_name = str(row.get("waste_stream", "") or "").strip()
        volume = self._row_float(
            row,
            "waste_volume_m3",
            "volume_filled_m3",
            "filled_volume_m3",
            "current_volume_m3",
            default=0.0,
        )
        capacity = self._row_float(
            row, "container_capacity_m3", "capacity_m3", default=0.0
        )
        if capacity <= 0.0 and stream_name:
            capacity = self._waste_stream_capacity_for_name(stream_name)

        if stream_name or volume > 0.0 or capacity > 0.0:
            if capacity > 0.0:
                percent = max(0.0, min(999.0, (volume / capacity) * 100.0))
                row["waste_volume_display"] = f"{volume:.3f} / {capacity:.3f} m³"
                row["fill_percent_display"] = f"{percent:.0f}%"
            else:
                row["waste_volume_display"] = f"{volume:.3f} m³"
                row["fill_percent_display"] = "-"
            row["waste_volume_m3"] = volume
            row["container_capacity_m3"] = capacity
        else:
            row.setdefault("waste_volume_display", "-")
            row.setdefault("fill_percent_display", "-")

        row["payload_instance_id"] = str(row.get("payload_instance_id", "") or "-")
        tooltip_lines = [
            f"Payload: {payload_name}",
            f"Details: {row.get('details', '-')}",
        ]
        if row.get("waste_volume_display", "-") != "-":
            tooltip_lines.append(
                f"Waste volume filled: {row.get('waste_volume_display')} ({row.get('fill_percent_display', '-')})"
            )
        threshold_display = str(row.get("threshold_display", "") or "").strip()
        if threshold_display:
            tooltip_lines.append(threshold_display)
        for label, key in [
            ("Fill rate", "fill_rate_display"),
            ("Waste stream", "waste_stream"),
            ("Container group", "container_group"),
            ("Instance", "payload_instance_id"),
            ("Task", "task_id"),
            ("Status", "status"),
            ("Source", "source"),
        ]:
            value = str(row.get(key, "") or "").strip()
            if value and value != "-":
                tooltip_lines.append(f"{label}: {value}")
        row["tooltip"] = "\n".join(tooltip_lines)
        return row

    def _waste_stream_name_for_payload_or_container(
        self, payload_name: str = "", container_type: str = ""
    ) -> str:
        """Infer the waste stream when a CSV row only gives the replacement payload.

        Return/drop-off rows for replacement containers do not always repeat the
        waste_stream column.  Seeded containers rely on the stream definition for
        the collection threshold, so infer the stream from the configured payload
        or container type before the row is enriched/drawn.
        """
        payload_name = str(payload_name or "").strip()
        container_type = str(container_type or "").strip()
        candidates = {x for x in (payload_name, container_type) if x}
        if not candidates:
            return ""

        for stream in self.layout_model.data.get("waste_streams", []) or []:
            if not isinstance(stream, dict):
                continue
            stream_name = str(stream.get("name", "") or "").strip()
            stream_payload = str(stream.get("payload", "") or "").strip()
            stream_container = str(stream.get("container_type", "") or "").strip()
            if stream_name and (
                stream_payload in candidates or stream_container in candidates
            ):
                return stream_name

        for department in self.layout_model.data.get("departments", []) or []:
            if not isinstance(department, dict):
                continue
            for stream_cfg in department.get("waste_streams", []) or []:
                if not isinstance(stream_cfg, dict):
                    continue
                stream_name = str(stream_cfg.get("name", "") or "").strip()
                stream_payload = str(stream_cfg.get("payload", "") or "").strip()
                stream_container = str(
                    stream_cfg.get("container_type", "") or ""
                ).strip()
                if stream_name and (
                    stream_payload in candidates or stream_container in candidates
                ):
                    return stream_name

        return ""

    def _csv_waste_row_payload_details(
        self, row: dict, reset_fill: bool = False
    ) -> dict:
        collected_volume = self._row_float(row, "waste_volume_m3", default=0.0)
        payload_name = str(row.get("payload", "") or "").strip()
        container_type = str(row.get("container_type", "") or "").strip()
        waste_stream = str(row.get("waste_stream", "") or "").strip()
        if not waste_stream:
            waste_stream = self._waste_stream_name_for_payload_or_container(
                payload_name, container_type
            )

        result = {
            "payload_instance_id": str(
                row.get("payload_instance_id", "") or ""
            ).strip(),
            "waste_stream": waste_stream,
            "collected_waste_volume_m3": collected_volume,
            "container_type": container_type,
        }
        if reset_fill:
            result["waste_volume_m3"] = 0.0
            result["fill_start_volume_m3"] = 0.0
        if result["waste_stream"]:
            result["container_capacity_m3"] = self._waste_stream_capacity_for_name(
                result["waste_stream"]
            )
            result["container_threshold_m3"] = self._waste_stream_threshold_for_name(
                result["waste_stream"]
            )
        return result

    def _best_empty_inventory_row_for_replacement(
        self, rows: List[dict], csv_row: dict
    ):
        """Prefer the empty slot that still carries the seeded waste metadata."""
        csv_details = self._csv_waste_row_payload_details(csv_row, reset_fill=True)
        stream_name = str(csv_details.get("waste_stream", "") or "").strip()
        container_group = str(csv_details.get("container_group", "") or "").strip()

        for row in rows:
            if str(row.get("payload", "-")).strip() not in {"", "-"}:
                continue
            if (
                stream_name
                and str(row.get("waste_stream", "") or "").strip() == stream_name
            ):
                return row
            if (
                container_group
                and str(row.get("container_group", "") or "").strip() == container_group
            ):
                return row

        for row in rows:
            if str(row.get("payload", "-")).strip() not in {"", "-"}:
                continue
            if row.get("container_threshold_m3") or row.get("container_capacity_m3"):
                return row

        return self._first_empty_inventory_space_row(rows)

    def _space_points_world(
        self, location: dict, space: dict
    ) -> List[Tuple[float, float]]:
        lx = float(location.get("x", 0.0) or 0.0)
        ly = float(location.get("y", 0.0) or 0.0)
        result = []
        for point in space.get("points", []) or []:
            if not isinstance(point, dict):
                continue
            try:
                if "dx" in point and "dy" in point:
                    result.append(
                        (
                            lx + float(point.get("dx", 0.0) or 0.0),
                            ly + float(point.get("dy", 0.0) or 0.0),
                        )
                    )
                else:
                    result.append(
                        (
                            float(point.get("x", lx) or lx),
                            float(point.get("y", ly) or ly),
                        )
                    )
            except Exception:
                continue
        return result

    def _space_centroid_world(self, location: dict, space: dict) -> Tuple[float, float]:
        points = self._space_points_world(location, space)
        if points:
            return (
                sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points),
            )
        return float(location.get("x", 0.0) or 0.0), float(
            location.get("y", 0.0) or 0.0
        )

    def _payload_slot_world_position(
        self, location: dict, space: dict, slot: dict
    ) -> Tuple[float, float]:
        lx = float(location.get("x", 0.0) or 0.0)
        ly = float(location.get("y", 0.0) or 0.0)
        if isinstance(slot, dict):
            try:
                if "dx" in slot and "dy" in slot:
                    return lx + float(slot.get("dx", 0.0) or 0.0), ly + float(
                        slot.get("dy", 0.0) or 0.0
                    )
                if "x" in slot and "y" in slot:
                    return float(slot.get("x", lx) or lx), float(
                        slot.get("y", ly) or ly
                    )
            except Exception:
                pass
        return self._space_centroid_world(location, space)

    def _slot_is_amr_slot(self, slot: dict) -> bool:
        return isinstance(slot, dict) and (
            str(slot.get("slot_type", "") or "").strip().lower() == "amr"
            or bool(str(slot.get("amr_type", "") or "").strip())
        )

    def _space_is_amr_space(self, space: dict) -> bool:
        if not isinstance(space, dict):
            return False
        if bool(space.get("stores_amr", False)):
            return True
        if str(space.get("space_type", "") or "").strip().lower() == "amr":
            return True
        if str(space.get("amr_type", "") or "").strip():
            return True
        return any(self._slot_is_amr_slot(slot) for slot in space.get("payload_slots", []) or [])

    def _amr_slot_rotation_deg(self, space: dict, fallback: float = 0.0) -> float:
        for slot in space.get("payload_slots", []) or []:
            if not self._slot_is_amr_slot(slot):
                continue
            try:
                return float(slot.get("rotation_deg", fallback) or fallback)
            except Exception:
                return float(fallback or 0.0)
        try:
            return float(space.get("rotation_deg", fallback) or fallback)
        except Exception:
            return float(fallback or 0.0)

    def _amr_type_for_amr_id(self, amr_id: str) -> str:
        amr_id = str(amr_id or "").strip()
        for amr_def in self.layout_model.data.get("amrs", []) or []:
            base = str(amr_def.get("id", "") or "").strip()
            if base and (amr_id == base or amr_id.startswith(base + "-")):
                return base
        parts = amr_id.rsplit("-", 1)
        return parts[0] if len(parts) == 2 and parts[1].isdigit() else amr_id

    def _amr_slot_type_for_space(self, space: dict) -> str:
        for slot in space.get("payload_slots", []) or []:
            if self._slot_is_amr_slot(slot):
                return str(slot.get("amr_type", "") or space.get("amr_type", "") or "").strip()
        return str(space.get("amr_type", "") or "").strip()

    def _space_is_compatible_with_amr_id(self, space: dict, amr_id: str) -> bool:
        amr_type = self._amr_type_for_amr_id(amr_id)
        slot_type = self._amr_slot_type_for_space(space)
        return self._space_is_amr_space(space) and (not slot_type or slot_type == amr_type)

    def _csv_initial_amr_home_space_map(self) -> Dict[str, dict]:
        return dict(getattr(self.sim_log, "initial_amr_home_spaces", {}) or {})

    @staticmethod
    def _natural_text_key(value: str):
        parts = []
        for part in re.split(r"(\d+)", str(value or "")):
            if part.isdigit():
                parts.append((0, int(part)))
            else:
                parts.append((1, part.lower()))
        return parts

    def _configured_amr_home_space_map(self) -> Dict[str, dict]:
        """Deterministically map AMR IDs to compatible AMR spaces for display fallback.

        New simulator logs include amr_inventory_space/amr_rotation_deg.  Older logs
        do not, so the visualiser uses the same layout data to infer each parked
        AMR bay instead of showing every bay as empty.
        """
        result: Dict[str, dict] = {}
        charge_locations = self.layout_model.data.get("building", {}).get("charge_locations", []) or []
        if isinstance(charge_locations, str):
            charge_locations = [charge_locations]
        if not charge_locations:
            fallback = str(self.layout_model.data.get("building", {}).get("amr_centre", "") or "").strip()
            if fallback:
                charge_locations = [fallback]

        locations_by_name = {str(loc.get("name", "") or "").strip(): loc for loc in self.layout_model.data.get("locations", []) or []}

        spaces_by_type: Dict[str, List[dict]] = {}
        for loc_name in charge_locations:
            loc = locations_by_name.get(str(loc_name or "").strip())
            if not loc:
                continue
            for idx, space in enumerate(loc.get("inventory_spaces", []) or []):
                if not isinstance(space, dict) or not self._space_is_amr_space(space):
                    continue
                amr_type = self._amr_slot_type_for_space(space)
                if not amr_type:
                    continue
                cx, cy = self._space_centroid_world(loc, space)
                rotation = self._amr_slot_rotation_deg(space, 0.0)
                spaces_by_type.setdefault(amr_type, []).append({
                    "location": str(loc_name),
                    "space": str(space.get("name", "") or f"AMR space {idx + 1}"),
                    "x": cx,
                    "y": cy,
                    "rotation_deg": rotation,
                })

        for amr_type, spaces in spaces_by_type.items():
            spaces.sort(
                key=lambda s: (
                    str(s.get("location", "")),
                    self._natural_text_key(str(s.get("space", ""))),
                )
            )

        for amr_def in self.layout_model.data.get("amrs", []) or []:
            amr_type = str(amr_def.get("id", "") or "").strip()
            if not amr_type:
                continue
            try:
                qty = int(float(amr_def.get("quantity", 1) or 1))
            except Exception:
                qty = 1
            compatible = spaces_by_type.get(amr_type, [])
            for idx in range(1, max(0, qty) + 1):
                if idx <= len(compatible):
                    result[f"{amr_type}-{idx}"] = compatible[idx - 1]
        return result

    def _amr_space_location_for_space_name(self, space_name: str, amr_id: str = "") -> str:
        """Return the charge location containing a named compatible AMR bay."""
        space_name = str(space_name or "").strip()
        if not space_name:
            return ""
        csv_home = self._csv_initial_amr_home_space_map().get(str(amr_id or "").strip())
        if csv_home and str(csv_home.get("space", "") or "").strip() == space_name:
            return str(csv_home.get("location", "") or "").strip()
        # Prefer the deterministic home map for this exact AMR.  This handles
        # initial rows that contain only amr_inventory_space/amr_rotation_deg.
        home = self._configured_amr_home_space_map().get(str(amr_id or "").strip())
        if home and str(home.get("space", "") or "").strip() == space_name:
            return str(home.get("location", "") or "").strip()

        charge_locations = self.layout_model.data.get("building", {}).get("charge_locations", []) or []
        if isinstance(charge_locations, str):
            charge_locations = [charge_locations]
        if not charge_locations:
            fallback = str(self.layout_model.data.get("building", {}).get("amr_centre", "") or "").strip()
            if fallback:
                charge_locations = [fallback]
        locations_by_name = {str(loc.get("name", "") or "").strip(): loc for loc in self.layout_model.data.get("locations", []) or []}
        for loc_name in charge_locations:
            loc = locations_by_name.get(str(loc_name or "").strip())
            if not loc:
                continue
            for space in loc.get("inventory_spaces", []) or []:
                if not isinstance(space, dict) or not self._space_is_amr_space(space):
                    continue
                if str(space.get("name", "") or "").strip() == space_name and self._space_is_compatible_with_amr_id(space, amr_id):
                    return str(loc_name or "").strip()
        return ""

    def _state_is_stationary_at_location(self, state: dict, location_name: str) -> bool:
        raw = state.get("raw", {}) or {}
        if state.get("start_node") and state.get("end_node") and state.get("path"):
            return False
        candidates = {
            str(state.get("to_location", "") or "").strip(),
            str(state.get("from_location", "") or "").strip(),
            str(raw.get("to_location", "") or "").strip(),
            str(raw.get("from_location", "") or "").strip(),
            str(raw.get("amr_location_after", "") or "").strip(),
            str(raw.get("end_node", "") or "").strip(),
        }
        status = str(state.get("status", "") or raw.get("status", "") or "").lower()
        event_type = str(state.get("event_type", "") or raw.get("event_type", "") or "").lower()
        return location_name in candidates or "charge" in status or "idle" in status or "return" in event_type

    def _current_amr_space_occupancy_by_location(self) -> Dict[str, Dict[str, dict]]:
        occupancy: Dict[str, Dict[str, dict]] = {}
        csv_home_map = self._csv_initial_amr_home_space_map()
        configured_home_map = self._configured_amr_home_space_map()
        try:
            amr_states, _recent = self._current_state()
        except Exception:
            return occupancy
        for amr_id, state in (amr_states or {}).items():
            raw = state.get("raw", {}) or {}
            amr_id = str(amr_id or state.get("amr_id", "") or raw.get("amr_id", "") or "").strip()
            if not amr_id:
                continue

            space_name = str(state.get("amr_inventory_space") or raw.get("amr_inventory_space") or "").strip()
            rotation = self._float_or_none(state.get("amr_rotation_deg"))
            if rotation is None:
                rotation = self._float_or_none(raw.get("amr_rotation_deg"))
            location_name = (
                str(raw.get("to_location", "") or "").strip()
                or str(raw.get("amr_location_after", "") or "").strip()
                or str(raw.get("end_node", "") or "").strip()
                or str(state.get("to_location", "") or "").strip()
            )
            if space_name and not location_name:
                location_name = self._amr_space_location_for_space_name(space_name, amr_id)

            if not space_name:
                home = csv_home_map.get(amr_id) or configured_home_map.get(amr_id)
                if home and self._state_is_stationary_at_location(state, str(home.get("location", ""))):
                    location_name = str(home.get("location", "") or "").strip()
                    space_name = str(home.get("space", "") or "").strip()
                    rotation = self._float_or_none(home.get("rotation_deg"))
                    # Move the drawn AMR footprint to the bay centre for old logs
                    # that only recorded the parent charge-location node.
                    state["x"] = float(home.get("x", state.get("x", 0.0)) or 0.0)
                    state["y"] = float(home.get("y", state.get("y", 0.0)) or 0.0)
                    state["amr_inventory_space"] = space_name
                    state["amr_rotation_deg"] = rotation

            if not space_name or not location_name:
                continue
            occupancy.setdefault(location_name, {})[space_name] = {
                "amr_id": amr_id,
                "state": state,
                "rotation_deg": rotation,
            }
        return occupancy

    def _payloads_from_display_value(self, value) -> List[str]:
        text = str(value or "").strip()
        if not text or text == "-":
            return []
        return [
            part.strip()
            for part in text.split(",")
            if part.strip() and part.strip() != "-"
        ]

    def _draw_inventory_space_status_at_world(
        self,
        location: dict,
        space: dict,
        row: Optional[dict] = None,
        suppress_empty_label: bool = False,
    ):
        """Draw the inventory-space boundary with its live occupancy state.

        The slot payload name is only a planned payload type/footprint.  The
        status shown here is derived from the current visualiser inventory row,
        which starts empty unless a seeded payload or CSV drop-off has actually
        placed something in the space.
        """
        points_world = self._space_points_world(location, space)
        if not points_world:
            return

        row = dict(row or {})
        payload_text = str(row.get("payload", "-") or "-").strip()
        occupied = bool(payload_text and payload_text != "-")
        status = str(
            row.get("status", "Occupied" if occupied else "Empty") or ""
        ).strip()
        status_lower = status.lower()

        points = [QPointF(*self.world_to_scene(x, y)) for x, y in points_world]
        item = QGraphicsPolygonItem(QPolygonF(points))

        if occupied:
            if "seed" in status_lower:
                fill = QColor(142, 68, 173, 45)
                outline = QColor("#c9a7ff")
            else:
                fill = QColor(46, 204, 113, 35)
                outline = QColor("#76f0a6")
        else:
            fill = QColor(120, 120, 120, 22)
            outline = QColor("#777777")

        item.setBrush(QBrush(fill))
        item.setPen(QPen(outline, 0.0))
        item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        item.setData(0, "inventory_space_status")
        item.setData(1, str(space.get("name", "") or ""))

        tooltip_lines = [
            f"Inventory space: {str(space.get('name', '') or '-')}",
            f"Status: {status or ('Occupied' if occupied else 'Empty')}",
            f"Payload: {payload_text if occupied else 'Empty'}",
        ]
        source = str(row.get("source", "") or "").strip()
        if source:
            tooltip_lines.append(f"Source: {source}")
        item.setToolTip("\n".join(tooltip_lines))
        self.graphics_scene.addItem(item)
        self._active_dynamic_items().append(item)

        if self.show_labels_check.isChecked() and not occupied and not suppress_empty_label:
            cx, cy = self._space_centroid_world(location, space)
            # Only empty spaces show their inventory-space name.  Occupied
            # spaces, including seeded waste containers, suppress this label so
            # the payload box text is the only text inside the occupied space.
            label = "Empty"
            name = str(space.get("name", "") or "").strip()
            if name:
                label = f"{name}: {label}"

            xs = [point[0] for point in points_world]
            ys = [point[1] for point in points_world]
            box_w = max(0.2, max(xs) - min(xs)) if xs else 0.8
            box_h = max(0.2, max(ys) - min(ys)) if ys else 0.5
            sx, sy = self.world_to_scene(cx, cy)
            label_item = self.draw_fitted_text_box_item(
                sx,
                sy,
                box_w * 0.94,
                box_h * 0.84,
                label,
                "#bdbdbd",
                dynamic=True,
                rotation_deg=0.0,
                max_lines=2,
                min_pixel_size=3,
                max_pixel_size=None,
                scene_height=0.10,
            )
            label_item.setToolTip("\n".join(tooltip_lines))

    def _draw_payload_box_at_world(
        self,
        x: float,
        y: float,
        payload_name: str,
        rotation_deg: float = 0.0,
        status: str = "Stored",
        source: str = "",
        row_details: Optional[dict] = None,
    ):
        if str(payload_name or "").startswith("AMR: "):
            length, width = self._amr_dimensions_for_name(str(payload_name).split(":", 1)[1].strip())
        else:
            length, width = self._payload_dimensions_for_name(payload_name)
        heading = math.radians(float(rotation_deg or 0.0))
        hl = length / 2.0
        hw = width / 2.0
        corners = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
        points = []
        for dx, dy in corners:
            rx = (dx * math.cos(heading)) - (dy * math.sin(heading))
            ry = (dx * math.sin(heading)) + (dy * math.cos(heading))
            sx, sy = self.world_to_scene(x + rx, y + ry)
            points.append(QPointF(sx, sy))

        item = QGraphicsPolygonItem(QPolygonF(points))
        status_lower = str(status or "").lower()
        if str(payload_name or "").startswith("AMR: "):
            fill = QColor(52, 152, 219, 145)
            outline = QColor("#d6ecff")
        elif "seed" in status_lower:
            fill = QColor(142, 68, 173, 160)
            outline = QColor("#e8d5ff")
        elif "empty" in status_lower:
            fill = QColor(90, 90, 90, 80)
            outline = QColor("#777777")
        else:
            fill = QColor(46, 204, 113, 145)
            outline = QColor("#d7ffe7")
        item.setBrush(self._texture_brush(fill, fill.alpha()))
        item.setPen(QPen(outline, 0.0))
        item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        item.setData(0, "room_payload")
        item.setData(1, payload_name)
        self.graphics_scene.addItem(item)
        self._active_dynamic_items().append(item)

        details = self._enrich_payload_row_details(
            row_details or {"payload": payload_name, "status": status, "source": source}
        )
        tooltip = details.get("tooltip", "")
        if tooltip:
            item.setToolTip(str(tooltip))

        if self.show_labels_check.isChecked():
            label_lines = []
            if str(payload_name or "").startswith("AMR: "):
                amr_label = str(payload_name).split(":", 1)[1].strip()
                label_lines.append(amr_label)
                status_label = str(status or "").strip()
                if status_label:
                    label_lines.append(status_label)
            else:
                label_lines.append(
                    f"Seeded {payload_name}" if "seed" in status_lower else payload_name
                )

            volume_label = str(details.get("waste_volume_display", "") or "").strip()
            fill_label = str(details.get("fill_percent_display", "") or "").strip()
            if volume_label and volume_label != "-":
                if fill_label and fill_label != "-":
                    label_lines.append(f"{fill_label} | {volume_label}")
                else:
                    label_lines.append(volume_label)

            threshold_label = str(details.get("threshold_display", "") or "").strip()
            if threshold_label:
                # Keep the in-box threshold line compact; the full text remains
                # available in the tooltip.
                threshold_label = threshold_label.replace("Trigger ", "Trig ")
                label_lines.append(threshold_label)

            label_text = "\n".join(line for line in label_lines if line)
            sx, sy = self.world_to_scene(x, y)
            label_item = self.draw_fitted_text_box_item(
                sx,
                sy,
                max(0.05, length * 0.96),
                max(0.05, width * 0.88),
                label_text,
                "#e8d5ff" if "seed" in status_lower else "#d7ffe7",
                dynamic=True,
                rotation_deg=rotation_deg,
                max_lines=4,
                min_pixel_size=3,
                max_pixel_size=None,
                scene_height=0.105,
            )
            if tooltip:
                label_item.setToolTip(str(tooltip))

    def _draw_seeded_fallback_rows_for_location(self, location: dict, rows: List[dict]):
        if not rows:
            return
        lx = float(location.get("x", 0.0) or 0.0)
        ly = float(location.get("y", 0.0) or 0.0)
        for idx, row in enumerate(rows[:8]):
            payload = str(row.get("payload", "") or "").strip()
            if not payload or payload == "-":
                continue
            offset_x = 0.75 + ((idx % 4) * 0.45)
            offset_y = 0.75 + ((idx // 4) * 0.45)
            self._draw_payload_box_at_world(
                lx + offset_x,
                ly + offset_y,
                payload,
                rotation_deg=0.0,
                status=str(row.get("status", "Seeded")),
                source=str(row.get("source", "")),
                row_details=row,
            )

    def draw_room_payloads_qt(self, floor: int):
        if hasattr(self, "show_room_payloads_check"):
            if not self.show_room_payloads_check.isChecked():
                return

        visible_world = self._visible_world_rect(margin_m=4.0)
        amr_occupancy_by_location = self._current_amr_space_occupancy_by_location()

        for location in self.layout_model.data.get("locations", []) or []:
            try:
                if int(location.get("floor", -999999)) != int(floor):
                    continue
            except Exception:
                continue

            if not self._location_intersects_visible_world(location, visible_world):
                continue

            location_name = str(location.get("name", "") or "").strip()
            if not location_name:
                continue

            rows = self._inventory_payload_rows_for_location(location_name)
            rows_by_space = {
                str(row.get("space", "") or "").strip(): row for row in rows
            }
            amr_occupancy_for_location = amr_occupancy_by_location.get(location_name, {})
            used_seeded_rows = set()
            seeded_row_indexes_by_payload: Dict[str, List[int]] = {}
            for row_index, seeded_row in enumerate(rows):
                if (
                    not str(seeded_row.get("source", "") or "")
                    .lower()
                    .startswith("seeded")
                ):
                    continue
                seeded_payload = str(seeded_row.get("payload", "") or "").strip()
                if seeded_payload and seeded_payload != "-":
                    seeded_row_indexes_by_payload.setdefault(seeded_payload, []).append(
                        row_index
                    )

            spaces = location.get("inventory_spaces", []) or []
            for space_index, space in enumerate(spaces, start=1):
                if not isinstance(space, dict):
                    continue
                space_name = (
                    str(space.get("name", "") or "").strip()
                    or f"Inventory {space_index}"
                )
                row = rows_by_space.get(space_name, {})
                amr_occupancy = amr_occupancy_for_location.get(space_name) if self._space_is_amr_space(space) else None
                if amr_occupancy:
                    occupied_amr_id = str(amr_occupancy.get("amr_id", "") or "").strip()
                    row = {
                        **(row or {}),
                        "space": space_name,
                        "payload": f"AMR: {occupied_amr_id}",
                        "amr_id": occupied_amr_id,
                        "status": "AMR parked",
                        "source": "Runtime AMR bay occupancy",
                    }
                row_payloads = self._payloads_from_display_value(row.get("payload", ""))
                row_status = str(row.get("status", "Empty") or "Empty")
                row_source = str(row.get("source", "") or "")

                # Always draw the inventory-space boundary and live occupancy
                # status.  Empty configured slots should remain visibly empty
                # until seeded or physically occupied by a CSV drop-off.
                self._draw_inventory_space_status_at_world(
                    location,
                    space,
                    row,
                    suppress_empty_label=bool(amr_occupancy),
                )

                slots = [
                    slot
                    for slot in (space.get("payload_slots", []) or [])
                    if isinstance(slot, dict)
                ]
                if not slots and row_payloads:
                    # Older layouts may have no payload slot coordinates.  Draw the
                    # first current payload in the centre of the inventory space.
                    cx, cy = self._space_centroid_world(location, space)
                    self._draw_payload_box_at_world(
                        cx,
                        cy,
                        row_payloads[0],
                        0.0,
                        row_status,
                        row_source,
                        row_details=row,
                    )
                    continue

                for slot_index, slot in enumerate(slots):
                    if self._slot_is_amr_slot(slot):
                        if amr_occupancy:
                            configured_payload = f"AMR: {str(amr_occupancy.get('amr_id', '') or '').strip()}"
                        else:
                            configured_payload = ""
                    else:
                        configured_payload = str(slot.get("payload", "") or "").strip()
                    payload_to_draw = configured_payload
                    details_row = row
                    draw_status = row_status
                    draw_source = row_source

                    # Waste payload slots are designated positions.  When a seeded
                    # waste container exists, draw the live seeded row in the first
                    # matching slot instead of drawing every configured slot as a
                    # static full bin.  This keeps the fill label live and avoids
                    # fake duplicate bins.
                    if configured_payload:
                        for seeded_idx in seeded_row_indexes_by_payload.get(
                            configured_payload, []
                        ):
                            if seeded_idx in used_seeded_rows:
                                continue
                            seeded_candidate = rows[seeded_idx]
                            details_row = seeded_candidate
                            payload_to_draw = configured_payload
                            draw_status = str(
                                seeded_candidate.get("status", "Seeded") or "Seeded"
                            )
                            draw_source = str(
                                seeded_candidate.get("source", "Seeded waste container")
                                or "Seeded waste container"
                            )
                            used_seeded_rows.add(seeded_idx)
                            break

                    if details_row is row:
                        if row_payloads:
                            if (
                                configured_payload
                                and configured_payload not in row_payloads
                            ):
                                # A CSV drop-off may have changed the payload in this space;
                                # draw the current payload instead of the configured default.
                                payload_to_draw = row_payloads[
                                    min(slot_index, len(row_payloads) - 1)
                                ]
                            elif not configured_payload:
                                payload_to_draw = row_payloads[
                                    min(slot_index, len(row_payloads) - 1)
                                ]
                        elif configured_payload:
                            # The row exists but has been emptied by a pickup event.
                            if row and str(row.get("payload", "-")).strip() in {
                                "",
                                "-",
                            }:
                                continue
                        else:
                            continue

                        # If seeded rows are present for this configured payload and
                        # they have all been consumed, do not draw extra static waste
                        # bins just because the slot definition contains a payload name.
                        if (
                            configured_payload
                            and configured_payload in seeded_row_indexes_by_payload
                            and str(details_row.get("source", "") or "")
                            .lower()
                            .startswith("layout")
                        ):
                            continue

                    if not payload_to_draw:
                        continue

                    x, y = self._payload_slot_world_position(location, space, slot)
                    try:
                        rotation = float(slot.get("rotation_deg", 0.0) or 0.0)
                    except Exception:
                        rotation = 0.0
                    self._draw_payload_box_at_world(
                        x,
                        y,
                        payload_to_draw,
                        rotation,
                        draw_status,
                        draw_source,
                        row_details=details_row,
                    )

            # If a seeded/shared container has no matching physical inventory slot,
            # keep showing it near the location as a fallback marker.
            fallback_rows = []
            for idx, row in enumerate(rows):
                if idx in used_seeded_rows:
                    continue
                if str(row.get("source", "")).lower().startswith("seeded"):
                    space_name = str(row.get("space", "") or "").strip()
                    if space_name not in rows_by_space or space_name.lower().startswith(
                        "seeded"
                    ):
                        fallback_rows.append(row)
            if fallback_rows:
                self._draw_seeded_fallback_rows_for_location(location, fallback_rows)

    def draw_seeded_waste_container_markers(self, floor: int):
        if hasattr(self, "show_seeded_waste_containers_check"):
            if not self.show_seeded_waste_containers_check.isChecked():
                return

        for name, point in self.layout_model.points_for_floor(floor).items():
            if point.get("kind") != "location":
                continue
            seeded_rows = self._seeded_waste_container_rows_for_location(name)
            if not seeded_rows:
                continue

            x, y = self.world_to_scene(point["x"], point["y"])
            count = len(seeded_rows)
            size = 0.35
            for idx, seeded in enumerate(seeded_rows[:6]):
                offset_x = 0.65 + ((idx % 3) * 0.42)
                offset_y = 0.65 + ((idx // 3) * 0.42)
                item = QGraphicsRectItem(x + offset_x, y + offset_y, size, size)
                item.setBrush(QBrush(QColor("#8e44ad")))
                item.setPen(QPen(QColor("#ffffff"), 0.0))
                item.setData(0, "seeded_waste_container")
                item.setData(1, name)
                self.graphics_scene.addItem(item)
                self.static_items.append(item)

            if self.show_labels_check.isChecked():
                label = f"{count} seeded bin" + ("s" if count != 1 else "")
                self.draw_text_item(
                    x + 1.0,
                    y + 1.15,
                    label,
                    "#e8d5ff",
                    ignore_transform=True,
                    pixel_size=max(6, self.get_text_pixel_size() - 1),
                )

    def _amr_dimensions_for_state(self, state: dict) -> Tuple[float, float]:
        raw = state.get("raw", {}) or {}
        amr_id = str(state.get("amr_id", "") or raw.get("amr_id", "") or "").strip()
        length, width = self._amr_dimensions_for_name(amr_id)
        return max(0.05, float(length)), max(0.05, float(width))

    def _normalise_angle_deg(self, value: float) -> float:
        return (float(value) + 180.0) % 360.0 - 180.0

    def _angle_lerp_deg(self, start_deg: float, end_deg: float, frac: float) -> float:
        frac = max(0.0, min(1.0, float(frac)))
        delta = self._normalise_angle_deg(float(end_deg) - float(start_deg))
        return float(start_deg) + (delta * frac)

    def _amr_heading_radians_for_state(self, state: dict) -> float:
        raw = state.get("raw", {}) or {}
        start_rot = state.get("amr_rotation_start_deg")
        end_rot = state.get("amr_rotation_end_deg")
        if start_rot is None:
            start_rot = self._float_or_none(raw.get("amr_rotation_start_deg"))
        if end_rot is None:
            end_rot = self._float_or_none(raw.get("amr_rotation_end_deg"))
        if start_rot is not None and end_rot is not None:
            return math.radians(self._angle_lerp_deg(float(start_rot), float(end_rot), float(state.get("segment_fraction", 1.0) or 0.0)))

        rotation = state.get("amr_rotation_deg")
        if rotation is None:
            rotation = self._float_or_none(raw.get("amr_rotation_deg"))
        if rotation is not None:
            return math.radians(float(rotation or 0.0))

        # Prefer the actual row segment coordinates over named graph nodes.
        # Some rows carry start_node/end_node for the wider route context while
        # start_x/start_y/end_x/end_y describe the specific animated leg.  Using
        # the node pair in those cases makes the AMR footprint appear 90 degrees
        # out on vertical/side approach legs.
        sx = self._float_or_none(raw.get("start_x"))
        sy = self._float_or_none(raw.get("start_y"))
        ex = self._float_or_none(raw.get("end_x"))
        ey = self._float_or_none(raw.get("end_y"))
        if sx is not None and sy is not None and ex is not None and ey is not None:
            dx = float(ex) - float(sx)
            dy = float(ey) - float(sy)
            if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                return math.atan2(dy, dx)

        if state.get("start_node") and state.get("end_node") and state.get("path"):
            if (
                state["start_node"] in self.layout_model.points
                and state["end_node"] in self.layout_model.points
            ):
                a = self.layout_model.points[state["start_node"]]
                b = self.layout_model.points[state["end_node"]]
                return math.atan2(
                    float(b["y"]) - float(a["y"]), float(b["x"]) - float(a["x"])
                )
        return 0.0

    def _state_is_stowed_in_amr_space(self, state: dict) -> bool:
        """True when the AMR is stationary in an AMR bay, so room-payload drawing owns the footprint/text."""
        raw = state.get("raw", {}) or {}
        if state.get("start_node") and state.get("end_node") and state.get("path"):
            return False
        space_name = str(state.get("amr_inventory_space") or raw.get("amr_inventory_space") or "").strip()
        if space_name:
            return True
        amr_id = str(state.get("amr_id", "") or raw.get("amr_id", "") or "").strip()
        home = self._csv_initial_amr_home_space_map().get(amr_id) or self._configured_amr_home_space_map().get(amr_id)
        if home and self._state_is_stationary_at_location(state, str(home.get("location", "") or "")):
            return True
        return False

    def _draw_amr_box_colored_qt(self, state: dict, fill="#4da3ff"):
        x = float(state["x"])
        y = float(state["y"])
        length, width = self._amr_dimensions_for_state(state)

        heading = self._amr_heading_radians_for_state(state)

        hl = length / 2.0
        hw = width / 2.0
        corners = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
        poly_pts = []
        for dx, dy in corners:
            rx = (dx * math.cos(heading)) - (dy * math.sin(heading))
            ry = (dx * math.sin(heading)) + (dy * math.cos(heading))
            sx, sy = self.world_to_scene(x + rx, y + ry)
            poly_pts.append(QPointF(sx, sy))

        poly = QGraphicsPolygonItem(QPolygonF(poly_pts))
        poly.setBrush(self._texture_brush(fill, 205))
        poly.setPen(QPen(QColor("#858585"), 0.0))
        poly.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        self.graphics_scene.addItem(poly)
        self._active_dynamic_items().append(poly)

        front_x = x + (hl * math.cos(heading))
        front_y = y + (hl * math.sin(heading))
        sx0, sy0 = self.world_to_scene(x, y)
        sx1, sy1 = self.world_to_scene(front_x, front_y)
        self.draw_line_item(sx0, sy0, sx1, sy1, "#858585", 0.0, dynamic=True)

    def build_lift_monitor_state(self) -> List[dict]:
        current_time = self.current_time
        lift_ids_key = tuple(
            str(lift.get("id", "Lift"))
            for lift in self.layout_model.data.get("lifts", [])
        )
        event_idx = (
            self.sim_log.event_index_at(current_time)
            if current_time and self.sim_log.events
            else 0
        )
        cache_key = (current_time, event_idx, lift_ids_key)
        if (
            self._lift_monitor_state_cache_key == cache_key
            and self._lift_monitor_state_cache_value is not None
        ):
            return copy.deepcopy(self._lift_monitor_state_cache_value)

        lifts = []

        for lift in self.layout_model.data.get("lifts", []):
            served_floors = sorted(int(x) for x in lift.get("served_floors", []))
            start_floor = int(
                lift.get("start_floor", served_floors[0] if served_floors else 0)
            )
            state = {
                "lift_id": lift.get("id", "Lift"),
                "served_floors": served_floors or [start_floor],
                "current_floor": float(start_floor),
                "occupant": None,
                "waiting_amrs": [],
            }

            if current_time and self.sim_log.events:
                waiting = set()
                last_floor = float(start_floor)
                active_travel = None
                active_occupant = None

                for event in self.sim_log.events_for_lift_until(
                    state["lift_id"], current_time
                ):
                    row = event.row

                    row_lift_id = (row.get("lift_id") or "").strip()
                    segment_type = (row.get("segment_type") or "").strip().lower()
                    event_type = (row.get("event_type") or "").strip().lower()
                    status = (row.get("status") or "").strip().lower()

                    start_node = (row.get("start_node") or "").strip()
                    end_node = (row.get("end_node") or "").strip()
                    from_location = (row.get("from_location") or "").strip()
                    to_location = (row.get("to_location") or "").strip()

                    lift_id = state["lift_id"]
                    lift_prefix = f"{lift_id}-f"

                    row_matches_lift = False

                    if row_lift_id == lift_id:
                        row_matches_lift = True
                    elif start_node.lower().startswith(lift_prefix):
                        row_matches_lift = True
                    elif end_node.lower().startswith(lift_prefix):
                        row_matches_lift = True
                    elif from_location.lower().startswith(lift_prefix):
                        row_matches_lift = True
                    elif to_location.lower().startswith(lift_prefix):
                        row_matches_lift = True

                    if not row_matches_lift:
                        continue

                    start_dt = event.start_time
                    end_dt = (
                        event.end_time
                        if event.end_time >= event.start_time
                        else event.start_time
                    )
                    if start_dt > current_time:
                        break

                    start_floor = self.sim_log._int_or_none(row.get("start_floor"))
                    end_floor = self.sim_log._int_or_none(row.get("end_floor"))
                    amr_id = (row.get("amr_id") or "").strip() or None
                    text_blob = " ".join(
                        x for x in [segment_type, event_type, status] if x
                    )

                    if end_dt <= current_time:
                        if start_floor is not None and end_floor is not None:
                            last_floor = float(end_floor)

                    is_reposition = "lift_reposition" in text_blob

                    is_travel = (
                        "lift_transfer" in text_blob
                        or "segment_lift" in text_blob
                        or (
                            row_lift_id
                            and start_floor is not None
                            and end_floor is not None
                            and start_floor != end_floor
                            and not is_reposition
                        )
                    )

                    is_waiting = any(
                        word in text_blob for word in ["wait", "queue", "board", "door"]
                    )

                    if start_dt <= current_time <= end_dt:
                        if (
                            (is_travel or is_reposition)
                            and start_floor is not None
                            and end_floor is not None
                        ):
                            total = max((end_dt - start_dt).total_seconds(), 0.001)
                            elapsed = max(
                                (current_time - start_dt).total_seconds(), 0.0
                            )
                            frac = max(0.0, min(1.0, elapsed / total))
                            active_travel = float(start_floor) + (
                                (float(end_floor) - float(start_floor)) * frac
                            )
                            if not is_reposition:
                                active_occupant = amr_id
                        elif is_waiting and amr_id:
                            waiting.add(amr_id)
                        elif row_lift_id and amr_id and "lift" in text_blob:
                            if not is_reposition:
                                active_occupant = amr_id

                state["current_floor"] = (
                    active_travel if active_travel is not None else last_floor
                )
                state["occupant"] = active_occupant
                if active_occupant in waiting:
                    waiting.discard(active_occupant)
                state["waiting_amrs"] = sorted(waiting)

            lifts.append(state)

        self._lift_monitor_state_cache_key = cache_key
        self._lift_monitor_state_cache_value = copy.deepcopy(lifts)
        return lifts

    def _clear_lift_monitor_dialog_reference(self, *_args):
        self.lift_monitor_dialog = None

    def _clear_amr_payload_monitor_dialog_reference(self, *_args):
        self.amr_payload_monitor_dialog = None

    def update_lift_monitor_dialog(self, force: bool = False):
        if self.lift_monitor_dialog is None:
            return
        if not self.lift_monitor_dialog.isVisible():
            return

        # The lift monitor used to rebuild from the whole event history every
        # dynamic scene refresh.  During playback that creates visible lag.
        # Limit monitor redraws to a small fixed UI rate; the main map remains
        # responsive and the lift panel is still effectively live.
        now = time.monotonic()
        if (
            not force
            and self.is_playing
            and (now - self._last_lift_monitor_update_wall_time)
            < self._lift_monitor_update_interval_sec
        ):
            return
        self._last_lift_monitor_update_wall_time = now

        lift_states = self.build_lift_monitor_state()
        self.lift_monitor_dialog.update_states(lift_states)
        if hasattr(self, "lift_dialog") and self.lift_dialog.isVisible():
            self.lift_dialog.update_from_time(self.current_time)

    def open_lift_monitor_dialog(self):
        lift_states = self.build_lift_monitor_state()
        if not lift_states:
            QMessageBox.information(
                self, "No lifts", "No lifts are defined in the loaded layout."
            )
            return

        # Reuse if already open
        if self.lift_monitor_dialog and self.lift_monitor_dialog.isVisible():
            self.lift_monitor_dialog.raise_()
            self.lift_monitor_dialog.activateWindow()
            return

        self.lift_monitor_dialog = LiftMonitorDialog(self)
        self.lift_monitor_dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self.lift_monitor_dialog.destroyed.connect(
            self._clear_lift_monitor_dialog_reference
        )
        self.lift_monitor_dialog.set_lifts(lift_states)
        self.lift_monitor_dialog.show()

    def _configured_amr_slot_summary_by_base_id(self) -> Dict[str, str]:
        summaries: Dict[str, str] = {}
        for amr_cfg in self.layout_model.data.get("amrs", []) or []:
            base_id = str(amr_cfg.get("id", "")).strip()
            if not base_id:
                continue
            slots = amr_cfg.get("payload_slots", []) or []
            if not isinstance(slots, list) or not slots:
                summaries[base_id] = "1/1"
                continue
            summaries[base_id] = f"0/{len(slots)}"
        return summaries

    def _base_amr_id_from_runtime_id(self, amr_id: str) -> str:
        amr_id = str(amr_id or "").strip()
        for amr_cfg in self.layout_model.data.get("amrs", []) or []:
            base_id = str(amr_cfg.get("id", "")).strip()
            if not base_id:
                continue
            if amr_id == base_id or amr_id.startswith(base_id + "-"):
                return base_id
        return amr_id.rsplit("-", 1)[0] if "-" in amr_id else amr_id

    def _split_csv_cell(self, value) -> List[str]:
        text = str(value or "").strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]

    def _parse_onboard_payloads_from_row(self, row: dict) -> Optional[List[dict]]:
        raw = str(row.get("onboard_payloads", "") or "").strip()
        if not raw:
            return None

        try:
            value = json.loads(raw)
        except Exception:
            return None

        if not isinstance(value, list):
            return None

        records = []
        for item in value:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id", "") or "").strip()
            payload = str(item.get("payload", "") or "").strip()
            if not task_id or not payload:
                continue
            records.append(
                {
                    "task_id": task_id,
                    "payload": payload,
                    "payload_instance_id": str(
                        item.get("payload_instance_id", "") or ""
                    ).strip(),
                    "from_location": str(
                        item.get("pickup", "")
                        or item.get("from_location", "")
                        or item.get("origin", "")
                        or ""
                    ).strip(),
                    "to_location": str(
                        item.get("dropoff", "")
                        or item.get("to_location", "")
                        or item.get("destination", "")
                        or ""
                    ).strip(),
                    "slot": str(
                        item.get("slot_name", "") or item.get("slot", "") or ""
                    ).strip(),
                }
            )
        return records

    def _records_from_grouped_payload_row(self, row: dict) -> List[dict]:
        """Build per-payload records from grouped multi-pickup CSV cells.

        The simulator writes grouped pickup/dropoff rows as comma-separated
        task_id/payload/payload_slot values.  The monitor needs one record per
        payload, not just the first cell value.
        """
        task_ids = self._split_csv_cell(row.get("task_id", ""))
        payloads = self._split_csv_cell(row.get("payload", ""))
        slots = self._split_csv_cell(
            row.get("payload_slot", "") or row.get("slot_name", "")
        )
        instances = self._split_csv_cell(row.get("payload_instance_id", ""))

        if not task_ids or not payloads:
            return []

        records = []
        for idx, task_id in enumerate(task_ids):
            payload = payloads[idx] if idx < len(payloads) else payloads[-1]
            if not payload:
                continue
            records.append(
                {
                    "task_id": task_id,
                    "payload": payload,
                    "payload_instance_id": (
                        instances[idx] if idx < len(instances) else ""
                    ),
                    "from_location": str(
                        row.get("from_location", "") or row.get("start_node", "") or ""
                    ).strip(),
                    "to_location": str(
                        row.get("to_location", "") or row.get("end_node", "") or ""
                    ).strip(),
                    "slot": slots[idx] if idx < len(slots) else "",
                }
            )
        return records

    def _format_onboard_payloads_for_label(self, row: dict) -> str:
        records = self._parse_onboard_payloads_from_row(row or {})
        if not records:
            records = self._records_from_grouped_payload_row(row or {})
        if not records:
            return ""
        parts = []
        for rec in records:
            payload = str(rec.get("payload", "") or "").strip()
            task_id = str(rec.get("task_id", "") or "").strip()
            slot = str(rec.get("slot", "") or "").strip()
            if not payload:
                continue
            text = payload
            if task_id:
                text = f"{text} ({task_id})"
            if slot:
                text = f"{slot}: {text}"
            parts.append(text)
        return ", ".join(parts)

    def build_amr_payload_monitor_rows(self) -> List[dict]:
        if not self.current_time or not self.sim_log.events:
            return []

        amr_states, _recent = self._current_state()
        onboard: Dict[str, Dict[str, dict]] = {}
        last_seen: Dict[str, datetime] = {}

        for event in self.sim_log.iter_events_until(self.current_time):
            row = event.row
            amr_id = str(row.get("amr_id", "") or "").strip()
            if not amr_id:
                continue

            last_seen[amr_id] = min(self.current_time, event.end_time)

            event_type = str(row.get("event_type", "") or "").strip().lower()
            segment_type = str(row.get("segment_type", "") or "").strip().lower()
            status = str(row.get("status", "") or "").strip().lower()
            text = " ".join([event_type, segment_type, status])

            # Authoritative state from the latest patched simulator.
            parsed_onboard = self._parse_onboard_payloads_from_row(row)
            if parsed_onboard is not None:
                amr_payloads = {}
                for item in parsed_onboard:
                    item = dict(item)
                    item["updated"] = event.start_time
                    amr_payloads[item["task_id"]] = item
                onboard[amr_id] = amr_payloads
                continue

            grouped_records = self._records_from_grouped_payload_row(row)
            amr_payloads = onboard.setdefault(amr_id, {})

            if grouped_records and (
                "pickup" in text or "pick_up" in text or "load" in text
            ):
                for item in grouped_records:
                    item = dict(item)
                    item["updated"] = event.start_time
                    amr_payloads[item["task_id"]] = item
                continue

            if grouped_records and (
                "dropoff" in text
                or "drop_off" in text
                or "unload" in text
                or "complete" in text
            ):
                for item in grouped_records:
                    amr_payloads.pop(item["task_id"], None)
                continue

            # Backwards-compatible inference for older single-payload CSVs.
            task_id = str(row.get("task_id", "") or "").strip()
            payload = str(row.get("payload", "") or "").strip()
            if not task_id or not payload:
                continue

            if "pickup" in text or "pick_up" in text or "load" in text:
                amr_payloads[task_id] = {
                    "task_id": task_id,
                    "payload": payload,
                    "from_location": str(
                        row.get("from_location", "") or row.get("start_node", "") or ""
                    ).strip(),
                    "to_location": str(
                        row.get("to_location", "") or row.get("end_node", "") or ""
                    ).strip(),
                    "slot": str(
                        row.get("payload_slot", "") or row.get("slot_name", "") or ""
                    ).strip(),
                    "updated": event.start_time,
                }
            elif (
                "dropoff" in text
                or "drop_off" in text
                or "unload" in text
                or "complete" in text
            ):
                amr_payloads.pop(task_id, None)

        amr_ids = sorted(set(amr_states.keys()) | set(last_seen.keys()))
        base_slot_summary = self._configured_amr_slot_summary_by_base_id()
        rows = []

        for amr_id in amr_ids:
            state = amr_states.get(amr_id, {})
            payload_records = list(onboard.get(amr_id, {}).values())
            payload_records.sort(
                key=lambda rec: (str(rec.get("slot", "")), str(rec.get("task_id", "")))
            )
            payload_text = ", ".join(
                f"{rec['slot'] + ': ' if rec.get('slot') else ''}{rec['payload']} ({rec['task_id']})"
                for rec in payload_records
            )
            task_text = ", ".join(rec["task_id"] for rec in payload_records)

            base_id = self._base_amr_id_from_runtime_id(amr_id)
            configured_slots = base_slot_summary.get(base_id, "")
            if configured_slots and "/" in configured_slots:
                total_slots = configured_slots.split("/", 1)[1]
                slots_text = f"{len(payload_records)}/{total_slots}"
            else:
                slots_text = str(len(payload_records))

            updated = last_seen.get(amr_id) or state.get("timestamp")
            rows.append(
                {
                    "amr_id": amr_id,
                    "payloads": payload_text or "-",
                    "payload_count": len(payload_records),
                    "slots": slots_text,
                    "task_ids": task_text or (state.get("task_id") or "-"),
                    "status": state.get("status") or state.get("event_type") or "-",
                    "segment_type": state.get("segment_type") or "-",
                    "from_location": state.get("from_location")
                    or state.get("start_node")
                    or "-",
                    "to_location": state.get("to_location")
                    or state.get("end_node")
                    or "-",
                    "floor": (
                        state.get("floor") if state.get("floor") is not None else "-"
                    ),
                    "updated": (
                        updated.strftime("%Y-%m-%d %H:%M:%S") if updated else "-"
                    ),
                }
            )

        return rows

    def update_amr_payload_monitor_dialog(self):
        if self.amr_payload_monitor_dialog is None:
            return
        if not self.amr_payload_monitor_dialog.isVisible():
            return
        self.amr_payload_monitor_dialog.update_states(
            self.build_amr_payload_monitor_rows(),
            self.current_time,
        )

    def open_amr_payload_monitor_dialog(self):
        if not self.sim_log.events:
            QMessageBox.information(
                self,
                "No simulation loaded",
                "Load a simulation CSV first.",
            )
            return

        if (
            self.amr_payload_monitor_dialog
            and self.amr_payload_monitor_dialog.isVisible()
        ):
            self.amr_payload_monitor_dialog.raise_()
            self.amr_payload_monitor_dialog.activateWindow()
            return

        self.amr_payload_monitor_dialog = AmrPayloadMonitorDialog(self)
        self.amr_payload_monitor_dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self.amr_payload_monitor_dialog.destroyed.connect(
            self._clear_amr_payload_monitor_dialog_reference
        )
        self.amr_payload_monitor_dialog.update_states(
            self.build_amr_payload_monitor_rows(),
            self.current_time,
        )
        self.amr_payload_monitor_dialog.show()

    def _node_name_at_view_event(self, event: QMouseEvent) -> Optional[str]:
        scene_pos = self.view.mapToScene(event.position().toPoint())

        # Only accept actual node marker items.
        # Do not accept labels because ItemIgnoresTransformations can make
        # their hit boxes behave incorrectly at different zoom levels.
        for item in self.graphics_scene.items(scene_pos):
            item_type = item.data(0)
            if item_type == "layout_node":
                node_name = item.data(1)
                if node_name:
                    return str(node_name)

        # Pixel-based fallback, locations only.
        floor = self.current_floor()
        cursor_view_pos = event.position()

        best_name = None
        best_dist_px = 12.0

        for name, point in self.layout_model.points_for_floor(floor).items():
            if point.get("kind") != "location":
                continue

            sx, sy = self.world_to_scene(point["x"], point["y"])
            point_view_pos = self.view.mapFromScene(QPointF(sx, sy))

            dist_px = math.hypot(
                float(point_view_pos.x()) - float(cursor_view_pos.x()),
                float(point_view_pos.y()) - float(cursor_view_pos.y()),
            )

            if dist_px <= best_dist_px:
                best_name = name
                best_dist_px = dist_px

        return best_name

    def _tasks_dropping_off_at_node(self, node_name: str) -> List[dict]:
        if not node_name:
            return []

        tasks = []
        for task in self.layout_model.data.get("tasks", []):
            dropoff = (task.get("dropoff") or "").strip()
            if dropoff != node_name:
                continue

            tasks.append(
                {
                    "id": (task.get("id") or "").strip() or "-",
                    "pickup": (task.get("pickup") or "").strip() or "-",
                    "dropoff": dropoff,
                    "payload": (task.get("payload") or "").strip() or "-",
                    "release_datetime": (task.get("release_datetime") or "").strip()
                    or "-",
                    "priority": str(task.get("priority", "-")),
                    "target_time": str(task.get("target_time") or "").strip() or "-",
                    "route_profile": (task.get("route_profile") or "").strip() or "-",
                }
            )

        tasks.sort(
            key=lambda item: (
                item["release_datetime"],
                item["id"],
            )
        )
        return tasks

    def _tasks_picking_up_at_node(self, node_name: str) -> List[dict]:
        if not node_name:
            return []

        tasks = []
        for task in self.layout_model.data.get("tasks", []):
            pickup = str(task.get("pickup") or "").strip()
            if pickup != node_name:
                continue

            tasks.append(
                {
                    "id": str(task.get("id") or "").strip() or "-",
                    "pickup": pickup,
                    "dropoff": str(task.get("dropoff") or "").strip() or "-",
                    "payload": str(task.get("payload") or "").strip() or "-",
                    "release_datetime": str(task.get("release_datetime") or "").strip()
                    or "-",
                    "priority": str(task.get("priority") or "-"),
                    "target_time": str(task.get("target_time") or "").strip() or "-",
                    "route_profile": str(task.get("route_profile") or "").strip()
                    or "-",
                }
            )

        tasks.sort(
            key=lambda item: (
                item["release_datetime"],
                item["id"],
            )
        )
        return tasks

    def _current_amrs_at_node(self, node_name: str) -> List[dict]:
        if not node_name or not self.current_time or not self.sim_log.events:
            return []

        node = self.layout_model.points.get(node_name)
        if not node:
            return []

        node_floor = int(node.get("floor", self.current_floor()))
        node_x = float(node["x"])
        node_y = float(node["y"])

        amr_states, _recent_events = self.sim_log.state_at(
            self.current_time,
            self.layout_model,
        )
        matches = []

        for state in amr_states.values():
            state_floor = state.get("floor")
            if state_floor is None or int(state_floor) != node_floor:
                continue

            at_node = False

            if (
                state.get("start_node") == node_name
                or state.get("end_node") == node_name
            ):
                if state.get("x") is None or state.get("y") is None:
                    at_node = True

            if state.get("x") is not None and state.get("y") is not None:
                dist = math.hypot(
                    float(state["x"]) - node_x,
                    float(state["y"]) - node_y,
                )
                if dist <= 0.75:
                    at_node = True

            if at_node:
                matches.append(state)

        matches.sort(key=lambda item: str(item.get("amr_id", "")))
        return matches

    def _location_by_name(self, location_name: str) -> Optional[dict]:
        for location in self.layout_model.data.get("locations", []):
            if str(location.get("name", "")).strip() == str(location_name).strip():
                return location
        return None

    def _bool_from_config_value(self, value, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "n", "off", "disabled"}:
            return False
        return bool(default)

    def _simulation_seed_location_payloads_enabled(self) -> bool:
        simulation = self.layout_model.data.get("simulation", {}) or {}
        return self._bool_from_config_value(
            simulation.get("seed_location_payloads_at_start", False),
            False,
        )

    def _space_seed_payloads_enabled(self, space: dict) -> bool:
        if not isinstance(space, dict):
            return False
        for key in (
            "seed_payload_at_start",
            "seed_location_payload_at_start",
            "seed_payloads_at_start",
            "seed_inventory_payloads_at_start",
            "initial_payload_present",
        ):
            if key in space:
                return self._bool_from_config_value(space.get(key), False)
        return self._simulation_seed_location_payloads_enabled()

    def _slot_seed_payload_enabled(self, slot: dict, space: dict) -> bool:
        if isinstance(slot, dict):
            for key in (
                "seed_payload_at_start",
                "seed_location_payload_at_start",
                "seed_payloads_at_start",
                "initial_payload_present",
            ):
                if key in slot:
                    return self._bool_from_config_value(slot.get(key), False)
        return self._space_seed_payloads_enabled(space)

    def _payload_value_from_space(self, space: dict):
        for key in (
            "current_payload",
            "payload",
            "payload_name",
            "stored_payload",
            "contents",
            "content",
            "item",
        ):
            value = space.get(key)
            if value not in (None, "", []):
                if isinstance(value, list):
                    return ", ".join(str(x) for x in value if str(x).strip()) or "-"
                return str(value)

        # payload_slots define where payloads can sit.  They are not current
        # occupancy unless location-payload seeding is explicitly enabled for
        # the space/slot or globally by simulation.seed_location_payloads_at_start.
        slots = space.get("payload_slots", []) or []
        if isinstance(slots, list):
            payloads = []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if not self._slot_seed_payload_enabled(slot, space):
                    continue
                payload = str(slot.get("payload", "") or "").strip()
                if payload:
                    payloads.append(payload)
            if payloads:
                return ", ".join(payloads)

        return "-"

    def _find_inventory_space_row(self, rows: List[dict], space_name: str):
        space_name = str(space_name or "").strip()
        if not space_name:
            return None
        for row in rows:
            if str(row.get("space", "")).strip() == space_name:
                return row
        return None

    def _find_inventory_row_by_payload_instance(
        self, rows: List[dict], instance_id: str
    ):
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            return None
        for row in rows:
            if str(row.get("payload_instance_id", "") or "").strip() == instance_id:
                return row
        return None

    def _find_seeded_inventory_row_for_payload_event(
        self, rows: List[dict], csv_row: dict
    ):
        payload = str(
            csv_row.get("payload", "") or csv_row.get("container_type", "") or ""
        ).strip()
        if not payload:
            return None

        csv_details = self._csv_waste_row_payload_details(csv_row)
        stream_name = str(csv_details.get("waste_stream", "") or "").strip()
        instance_id = str(csv_details.get("payload_instance_id", "") or "").strip()

        for row in rows:
            if not str(row.get("source", "") or "").lower().startswith("seeded"):
                continue
            if str(row.get("payload", "") or "").strip() != payload:
                continue
            row_instance_id = str(row.get("payload_instance_id", "") or "").strip()
            if row_instance_id and instance_id and row_instance_id != instance_id:
                continue
            row_stream = str(row.get("waste_stream", "") or "").strip()
            if stream_name and row_stream and row_stream != stream_name:
                continue
            return row
        return None

    def _first_empty_inventory_space_row(self, rows: List[dict]):
        for row in rows:
            if str(row.get("payload", "-")).strip() in {"", "-"}:
                return row
        return None

    def _event_location_matches(
        self, row: dict, location_name: str, event_kind: str
    ) -> bool:
        location_name = str(location_name or "").strip()
        if not location_name:
            return False

        field_groups = {
            "dropoff": [
                "to_location",
                "dropoff",
                "end_node",
                "destination",
                "location",
            ],
            "pickup": ["from_location", "pickup", "start_node", "origin", "location"],
        }
        for key in field_groups.get(event_kind, []):
            if str(row.get(key, "")).strip() == location_name:
                return True
        return False

    def _inventory_physical_payload_event_kind(self, row: dict) -> str:
        """Return pickup/dropoff only for rows that physically move a payload.

        Planning rows such as task_generated, return_task_generated,
        waste_task_generated and task_assigned can contain a payload, pickup and
        drop-off location, but the payload has not moved yet.  Treating those
        rows as inventory updates makes a return bin appear in the department
        slot as soon as the return task is created rather than when it is
        delivered.
        """
        event_type = str(row.get("event_type", "") or "").strip().lower()
        segment_type = str(row.get("segment_type", "") or "").strip().lower()
        status = str(row.get("status", "") or "").strip().lower()
        amr_id = str(row.get("amr_id", "") or "").strip()

        if event_type == "location_payload_enter":
            return "dropoff"
        if event_type == "location_payload_exit":
            return "pickup"

        # Generated rows and assignment rows are task metadata, not physical
        # inventory movement.  Most generated rows also have no AMR id.
        if (
            event_type
            in {
                "task_generated",
                "return_task_generated",
                "waste_task_generated",
                "task_assigned",
                "multi_stop_task_assigned",
            }
            or event_type.endswith("_generated")
            or (status == "generated" and not amr_id)
        ):
            return ""

        physical_text = " ".join([event_type, segment_type])
        if any(
            token in physical_text
            for token in ["dropoff", "drop_off", "deliver", "delivery", "unload"]
        ):
            return "dropoff"
        if any(
            token in physical_text for token in ["pickup", "pick_up", "collect", "load"]
        ):
            return "pickup"
        return ""

    def _inventory_space_name_from_event(self, row: dict, event_kind: str) -> str:
        keys = [
            "inventory_space",
            "inventory_space_name",
            "space",
            "space_name",
        ]
        if event_kind == "dropoff":
            keys = ["to_inventory_space", "dropoff_inventory_space"] + keys
        elif event_kind == "pickup":
            keys = ["from_inventory_space", "pickup_inventory_space"] + keys
        for key in keys:
            value = str(row.get(key, "")).strip()
            if value:
                return value
        details = str(row.get("details", "") or "").strip()
        if details.startswith("{"):
            try:
                payload = json.loads(details)
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                for key in keys:
                    value = str(payload.get(key, "") or "").strip()
                    if value:
                        return value
        return ""

    def _simulation_seed_waste_containers_enabled(self) -> bool:
        simulation = self.layout_model.data.get("simulation", {}) or {}
        return bool(simulation.get("seed_waste_stream_containers_at_start", False))

    def _waste_stream_lookup(self) -> Dict[str, dict]:
        result = {}
        for stream in self.layout_model.data.get("waste_streams", []) or []:
            if not isinstance(stream, dict):
                continue
            name = str(stream.get("name", "") or "").strip()
            if name:
                result[name] = stream
        return result

    def _department_id_for_item(self, department: dict) -> str:
        return (
            str(department.get("id", "") or "").strip()
            or str(department.get("name", "") or "").strip()
        )

    def _waste_locations_for_department(self, department: dict) -> List[str]:
        locations = []
        tg_locations = department.get("task_generation_locations", {}) or {}
        waste_entry = (
            tg_locations.get("waste", {}) if isinstance(tg_locations, dict) else {}
        )

        if isinstance(waste_entry, dict):
            raw = waste_entry.get(
                "pickup_dropoff_locations", waste_entry.get("locations", [])
            )
        else:
            raw = waste_entry

        if isinstance(raw, list):
            locations.extend(str(x).strip() for x in raw if str(x).strip())
        elif raw:
            locations.append(str(raw).strip())

        for key in ("waste_pickup_locations", "waste_locations"):
            raw_extra = department.get(key, [])
            if isinstance(raw_extra, list):
                locations.extend(str(x).strip() for x in raw_extra if str(x).strip())

        return sorted(set(locations))

    def _seeded_waste_container_rows_for_location(
        self, location_name: str
    ) -> List[dict]:
        if hasattr(self, "show_seeded_waste_containers_check"):
            if not self.show_seeded_waste_containers_check.isChecked():
                return []

        location_name = str(location_name or "").strip()
        if not location_name:
            return []

        global_seed_enabled = self._simulation_seed_waste_containers_enabled()
        stream_lookup = self._waste_stream_lookup()
        container_rows: Dict[tuple, dict] = {}

        for department in self.layout_model.data.get("departments", []) or []:
            if not isinstance(department, dict):
                continue
            department_locations = self._waste_locations_for_department(department)
            if location_name not in department_locations:
                continue

            dept_id = self._department_id_for_item(department)
            dept_name = str(department.get("name", "") or "").strip() or dept_id

            for stream_cfg in department.get("waste_streams", []) or []:
                if not isinstance(stream_cfg, dict):
                    continue

                stream_name = str(stream_cfg.get("name", "") or "").strip()
                if not stream_name:
                    continue

                initial_present = stream_cfg.get("initial_container_present", None)
                if initial_present is None:
                    initial_present = global_seed_enabled
                if not bool(initial_present):
                    continue

                stream_def = stream_lookup.get(stream_name, {})
                payload = (
                    str(stream_cfg.get("payload", "") or "").strip()
                    or str(stream_def.get("payload", "") or "").strip()
                    or str(stream_def.get("container_type", "") or "").strip()
                    or stream_name
                )

                shared = bool(
                    stream_cfg.get(
                        "shared_container", stream_cfg.get("shared_bin", False)
                    )
                )
                shared_group = str(
                    stream_cfg.get("shared_container_group", "")
                    or stream_cfg.get("shared_bin_group", "")
                    or ""
                ).strip()

                if shared:
                    container_group = shared_group or f"{stream_name}:{location_name}"
                    container_key = ("shared", stream_name, container_group)
                else:
                    container_group = f"{dept_id}:{stream_name}:{location_name}"
                    container_key = ("department", dept_id, stream_name, location_name)

                capacity_m3 = self._waste_stream_capacity_for_name(stream_name)
                threshold_m3 = self._waste_stream_threshold_for_name(stream_name)
                initial_volume_m3 = self._row_float(
                    stream_cfg,
                    "initial_waste_volume_m3",
                    "initial_volume_m3",
                    "current_volume_m3",
                    "waste_volume_m3",
                    default=0.0,
                )
                daily_rate_m3 = self._waste_stream_daily_volume_m3(stream_cfg)
                contributor = {
                    "department_id": dept_id,
                    "department_name": dept_name,
                    "daily_rate_m3_per_day": daily_rate_m3,
                    "operating_start_time": str(
                        department.get("operating_start_time", "") or ""
                    ),
                    "operating_end_time": str(
                        department.get("operating_end_time", "") or ""
                    ),
                    "days_active": list(department.get("days_active", []) or []),
                    "enabled": bool(department.get("enabled", True)),
                }

                existing = container_rows.get(container_key)
                if existing is None:
                    container_rows[container_key] = {
                        "space": f"Seeded {stream_name} container",
                        "payload": payload,
                        "payload_instance_id": str(
                            stream_cfg.get("payload_instance_id", "") or ""
                        ),
                        "task_id": "-",
                        "amr_id": "-",
                        "status": "Seeded",
                        "timestamp": "Simulation start",
                        "fill_start_time": "Simulation start",
                        "source": "Seeded waste container",
                        "waste_stream": stream_name,
                        "waste_volume_m3": initial_volume_m3,
                        "fill_start_volume_m3": initial_volume_m3,
                        "live_waste_volume_m3_per_day": daily_rate_m3,
                        "container_capacity_m3": capacity_m3,
                        "container_threshold_m3": threshold_m3,
                        "container_group": container_group,
                        "department_id": dept_id,
                        "department_name": dept_name,
                        "departments_served": dept_name,
                        "live_waste_contributors": [contributor],
                    }
                else:
                    # Shared containers accumulate the generated volume from all
                    # departments that use the same shared bin group/location.
                    existing["live_waste_volume_m3_per_day"] = (
                        float(existing.get("live_waste_volume_m3_per_day", 0.0) or 0.0)
                        + daily_rate_m3
                    )
                    existing["fill_start_volume_m3"] = (
                        float(existing.get("fill_start_volume_m3", 0.0) or 0.0)
                        + initial_volume_m3
                    )
                    existing["waste_volume_m3"] = existing["fill_start_volume_m3"]
                    if capacity_m3 > 0.0 and not existing.get("container_capacity_m3"):
                        existing["container_capacity_m3"] = capacity_m3
                    if threshold_m3 > 0.0 and not existing.get(
                        "container_threshold_m3"
                    ):
                        existing["container_threshold_m3"] = threshold_m3
                    names = [
                        x.strip()
                        for x in str(existing.get("departments_served", "")).split(",")
                        if x.strip()
                    ]
                    if dept_name not in names:
                        names.append(dept_name)
                    existing["departments_served"] = ", ".join(names)
                    contributors = existing.setdefault("live_waste_contributors", [])
                    if isinstance(contributors, list):
                        contributors.append(contributor)

        return [
            self._enrich_payload_row_details(row) for row in container_rows.values()
        ]

    def _parse_mass_collection_instance_list(self, details: str, key: str) -> List[str]:
        details = str(details or "")
        marker = f"{key}="
        if marker not in details:
            return []
        tail = details.split(marker, 1)[1].strip()
        # The simulator writes values like collected=['id1', 'id2']; stop at the
        # matching list close before the next semicolon/text block.
        start = tail.find("[")
        end = tail.find("]", start + 1)
        if start < 0 or end < 0:
            return []
        try:
            parsed = ast.literal_eval(tail[start : end + 1])
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(x).strip() for x in parsed if str(x).strip()]

    def _apply_mass_collection_inventory_event(
        self,
        rows: List[dict],
        csv_row: dict,
        event_time: datetime,
        location_name: str,
    ) -> None:
        if not rows:
            return
        if not self._event_location_matches(
            csv_row, location_name, "pickup"
        ) and not self._event_location_matches(csv_row, location_name, "dropoff"):
            return

        payload = str(
            csv_row.get("payload", "") or csv_row.get("container_type", "") or ""
        ).strip()
        details = str(csv_row.get("details", "") or "")
        collected_ids = self._parse_mass_collection_instance_list(details, "collected")
        replacement_ids = self._parse_mass_collection_instance_list(
            details, "replacements"
        )
        fallback_replacement = str(csv_row.get("payload_instance_id", "") or "").strip()
        if fallback_replacement and fallback_replacement not in replacement_ids:
            replacement_ids.append(fallback_replacement)

        timestamp = event_time.strftime("%Y-%m-%d %H:%M:%S")

        # Remove the used/full bins that the third-party rotation has taken away.
        removed = 0
        for instance_id in collected_ids:
            target = self._find_inventory_row_by_payload_instance(rows, instance_id)
            if target is None and payload:
                for candidate in rows:
                    if str(
                        candidate.get("payload", "") or ""
                    ).strip() == payload and str(
                        candidate.get("status", "") or ""
                    ).lower() not in {
                        "empty",
                        "available empty",
                    }:
                        target = candidate
                        break
            if target is None:
                continue
            target.update(
                {
                    "payload": "-",
                    "payload_instance_id": "-",
                    "task_id": "-",
                    "amr_id": "-",
                    "status": "Empty",
                    "timestamp": timestamp,
                    "fill_start_time": timestamp,
                    "source": "Mass collection",
                    "waste_volume_m3": 0.0,
                    "fill_start_volume_m3": 0.0,
                }
            )
            target.update(self._enrich_payload_row_details(target))
            removed += 1

        # Place the empty equivalents delivered by the third party into empty
        # inventory spaces.  For stores without explicit spaces the visualiser
        # keeps a single fallback row, so showing the latest empty replacement is
        # still more accurate than leaving the store as permanently empty.
        for instance_id in replacement_ids:
            target = self._best_empty_inventory_row_for_replacement(rows, csv_row)
            if target is None and rows:
                target = rows[0]
            if target is None:
                continue

            csv_details = self._csv_waste_row_payload_details(csv_row, reset_fill=True)
            for preserve_key in (
                "waste_stream",
                "container_capacity_m3",
                "container_threshold_m3",
                "container_group",
                "live_waste_volume_m3_per_day",
                "departments_served",
                "live_waste_contributors",
            ):
                if not csv_details.get(preserve_key) and target.get(preserve_key):
                    csv_details[preserve_key] = target.get(preserve_key)

            target.update(
                {
                    "payload": payload or target.get("payload", "-"),
                    "payload_instance_id": instance_id,
                    "task_id": str(csv_row.get("task_id", "") or "-").strip() or "-",
                    "amr_id": "-",
                    "status": "Available empty",
                    "timestamp": timestamp,
                    "fill_start_time": timestamp,
                    "source": "Mass collection",
                    **csv_details,
                }
            )
            target.update(self._enrich_payload_row_details(target))

    def _inventory_payload_rows_for_location(self, location_name: str) -> List[dict]:
        cache_key = self._inventory_cache_key_for_location(location_name)
        cached = self._inventory_rows_cache.get(cache_key)
        if cached is not None:
            return [dict(row) for row in cached]

        location = self._location_by_name(location_name)
        if not location:
            return []

        spaces = location.get("inventory_spaces", []) or []
        rows = []

        if spaces:
            for idx, space in enumerate(spaces, start=1):
                space_name = str(space.get("name", "")).strip() or f"Inventory {idx}"
                payload = self._payload_value_from_space(space)

                rows.append(
                    self._enrich_payload_row_details(
                        {
                            "space": space_name,
                            "payload": payload,
                            "payload_instance_id": space.get("payload_instance_id", ""),
                            "task_id": space.get("task_id", "-"),
                            "amr_id": space.get("amr_id", "-"),
                            "status": "Stored" if payload != "-" else "Empty",
                            "timestamp": space.get("timestamp", "-"),
                            "source": "Layout JSON",
                        }
                    )
                )
        else:
            # No defined inventory spaces: still show the location contents.
            rows.append(
                self._enrich_payload_row_details(
                    {
                        "space": "Location",
                        "payload": "-",
                        "task_id": "-",
                        "amr_id": "-",
                        "status": "Empty",
                        "timestamp": "-",
                        "source": "Location fallback",
                    }
                )
            )

        slot_payloads_by_space: Dict[str, set] = {}
        for idx, space in enumerate(spaces, start=1):
            if not isinstance(space, dict):
                continue
            space_name = str(space.get("name", "")).strip() or f"Inventory {idx}"
            slot_payloads = set()
            for slot in space.get("payload_slots", []) or []:
                if not isinstance(slot, dict):
                    continue
                slot_payload = str(slot.get("payload", "") or "").strip()
                if slot_payload:
                    slot_payloads.add(slot_payload)
            slot_payloads_by_space[space_name] = slot_payloads

        def row_allows_payload(row: dict, payload_name: str) -> bool:
            payload_name = str(payload_name or "").strip()
            if not payload_name:
                return True
            space_name = str(row.get("space", "") or "").strip()
            slot_payloads = slot_payloads_by_space.get(space_name, set())
            return not slot_payloads or payload_name in slot_payloads

        def compatible_space_row(space_name: str, payload_name: str):
            target = self._find_inventory_space_row(rows, space_name)
            if target is not None and not row_allows_payload(target, payload_name):
                return None
            return target

        def compatible_empty_row(csv_row: dict):
            payload_name = str(
                csv_row.get("payload", "")
                or csv_row.get("container_type", "")
                or ""
            ).strip()
            target = self._best_empty_inventory_row_for_replacement(rows, csv_row)
            if target is not None and row_allows_payload(target, payload_name):
                return target
            for candidate in rows:
                if str(candidate.get("payload", "-")).strip() not in {"", "-"}:
                    continue
                if row_allows_payload(candidate, payload_name):
                    return candidate
            return None

        seeded_rows = self._seeded_waste_container_rows_for_location(location_name)
        if seeded_rows:
            # Seeded containers should occupy a real inventory space when the
            # location has a matching payload slot.  Previously a seeded bin was
            # appended as a virtual row whenever the matching space was empty,
            # so the space still looked empty and showed its space-name label.
            # Build a small index of configured slot payloads by space so seeded
            # rows can be placed into the intended physical box.
            def apply_seeded_to_row(target: dict, seeded: dict) -> None:
                target.update(
                    {
                        "payload": str(
                            seeded.get("payload", "") or target.get("payload", "-")
                        ).strip()
                        or "-",
                        "status": "Seeded",
                        "timestamp": "Simulation start",
                        "fill_start_time": seeded.get(
                            "fill_start_time", "Simulation start"
                        ),
                        "source": "Seeded waste container",
                        "waste_stream": seeded.get("waste_stream", ""),
                        "waste_volume_m3": seeded.get("waste_volume_m3", 0.0),
                        "fill_start_volume_m3": seeded.get(
                            "fill_start_volume_m3",
                            seeded.get("waste_volume_m3", 0.0),
                        ),
                        "live_waste_volume_m3_per_day": seeded.get(
                            "live_waste_volume_m3_per_day", 0.0
                        ),
                        "live_waste_contributors": seeded.get(
                            "live_waste_contributors", []
                        ),
                        "container_capacity_m3": seeded.get(
                            "container_capacity_m3", 0.0
                        ),
                        "container_threshold_m3": seeded.get(
                            "container_threshold_m3", 0.0
                        ),
                        "container_group": seeded.get("container_group", ""),
                        "departments_served": seeded.get(
                            "departments_served",
                            target.get("departments_served", ""),
                        ),
                        "payload_instance_id": seeded.get(
                            "payload_instance_id",
                            target.get("payload_instance_id", ""),
                        ),
                    }
                )
                target.update(self._enrich_payload_row_details(target))

            for seeded in seeded_rows:
                existing = None
                seeded_payload = str(seeded.get("payload", "") or "").strip()

                # 1) Reuse a row that already contains this payload.
                for candidate in rows:
                    candidate_payload = str(candidate.get("payload", "") or "").strip()
                    if seeded_payload and candidate_payload == seeded_payload:
                        existing = candidate
                        break

                # 2) Otherwise occupy the first empty physical space whose slot
                # is configured for this payload type.
                if existing is None and seeded_payload:
                    for candidate in rows:
                        candidate_payload = str(
                            candidate.get("payload", "-") or "-"
                        ).strip()
                        if candidate_payload not in {"", "-"}:
                            continue
                        space_name = str(candidate.get("space", "") or "").strip()
                        if seeded_payload in slot_payloads_by_space.get(
                            space_name, set()
                        ):
                            existing = candidate
                            break

                # 3) If the location has spaces but no explicit payload match,
                # still use an empty physical row before falling back to a
                # virtual marker.  This keeps seeded containers inside spaces.
                if existing is None and spaces:
                    for candidate in rows:
                        candidate_payload = str(
                            candidate.get("payload", "-") or "-"
                        ).strip()
                        if candidate_payload in {"", "-"}:
                            existing = candidate
                            break

                if existing is not None:
                    apply_seeded_to_row(existing, seeded)
                else:
                    rows.append(seeded)

        if not self.current_time or not self.sim_log.events:
            result = [self._enrich_payload_row_details(row) for row in rows]
            self._inventory_rows_cache[cache_key] = [dict(row) for row in result]
            self._trim_inventory_rows_cache()
            return [dict(row) for row in result]

        for event in self.sim_log.events_for_location_until(
            location_name, self.current_time
        ):
            row = event.row
            if (
                str(row.get("event_type", "") or "").strip().lower()
                == "mass_collection_visit"
            ):
                self._apply_mass_collection_inventory_event(
                    rows, row, event.start_time, location_name
                )
                continue

            physical_event_kind = self._inventory_physical_payload_event_kind(row)
            is_dropoff = physical_event_kind == "dropoff"
            is_pickup = physical_event_kind == "pickup"

            payload_from_row = str(
                row.get("payload", "") or row.get("container_type", "") or ""
            ).strip()
            has_waste_volume = str(
                row.get("waste_volume_m3", "") or ""
            ).strip() not in {"", "0", "0.0", "0.000"}
            is_waste_update = bool(
                payload_from_row
                and str(row.get("waste_stream", "") or "").strip()
                and has_waste_volume
            )

            # Planning rows such as task_generated/task_assigned carry the collected
            # volume for the future collection.  They must not change the live fill
            # of the bin in the room.  Only physical pickup/dropoff rows move/reset
            # containers.
            if (
                False
                and (not is_dropoff and not is_pickup)
                and is_waste_update
                and self._event_location_matches(row, location_name, "pickup")
            ):
                target = None
                for candidate in rows:
                    candidate_payload = str(candidate.get("payload", "") or "").strip()
                    candidate_stream = str(
                        candidate.get("waste_stream", "") or ""
                    ).strip()
                    if (
                        candidate_payload == payload_from_row
                        or candidate_stream
                        == str(row.get("waste_stream", "") or "").strip()
                    ):
                        target = candidate
                        break
                if target is None:
                    target = self._first_empty_inventory_space_row(rows)
                if target is not None:
                    target.update(
                        {
                            "payload": payload_from_row,
                            "task_id": str(row.get("task_id", "") or "-").strip()
                            or "-",
                            "amr_id": str(row.get("amr_id", "") or "-").strip() or "-",
                            "status": "Filled",
                            "timestamp": event.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "fill_start_time": event.start_time.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "source": "Simulation CSV",
                            **self._csv_waste_row_payload_details(row),
                        }
                    )
                    target.update(self._enrich_payload_row_details(target))
                continue

            if not is_dropoff and not is_pickup:
                continue

            if is_dropoff and self._event_location_matches(
                row, location_name, "dropoff"
            ):
                instance_id = str(row.get("payload_instance_id", "") or "").strip()
                payload_for_event = str(
                    row.get("payload", "") or row.get("container_type", "") or ""
                ).strip()
                target = (
                    self._find_inventory_row_by_payload_instance(rows, instance_id)
                    or compatible_space_row(
                        self._inventory_space_name_from_event(row, "dropoff"),
                        payload_for_event,
                    )
                    or self._find_seeded_inventory_row_for_payload_event(rows, row)
                    or compatible_empty_row(row)
                )
                if target is None:
                    continue

                # A returned waste bin has been emptied at the waste destination,
                # so when it comes back to the department its fill restarts from
                # zero.  Preserve the seeded row's waste stream/rate metadata when
                # the return CSV row does not carry it.
                csv_details = self._csv_waste_row_payload_details(row, reset_fill=True)
                for preserve_key in (
                    "waste_stream",
                    "container_capacity_m3",
                    "container_threshold_m3",
                    "container_group",
                    "live_waste_volume_m3_per_day",
                    "departments_served",
                    "live_waste_contributors",
                ):
                    if not csv_details.get(preserve_key) and target.get(preserve_key):
                        csv_details[preserve_key] = target.get(preserve_key)

                target.update(
                    {
                        "payload": str(row.get("payload", "")).strip()
                        or str(row.get("container_type", "")).strip()
                        or "-",
                        "task_id": str(row.get("task_id", "")).strip() or "-",
                        "amr_id": str(row.get("amr_id", "")).strip() or "-",
                        "status": (
                            "Returned empty"
                            if str(row.get("task_id", "")).upper().startswith("RETURN")
                            else "Occupied"
                        ),
                        "timestamp": event.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "fill_start_time": event.start_time.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "source": "Simulation CSV",
                        **csv_details,
                    }
                )
                target.update(self._enrich_payload_row_details(target))

            if is_pickup and self._event_location_matches(row, location_name, "pickup"):
                payload = str(row.get("payload", "")).strip()
                instance_id = str(row.get("payload_instance_id", "") or "").strip()
                target = (
                    self._find_inventory_row_by_payload_instance(rows, instance_id)
                    or compatible_space_row(
                        self._inventory_space_name_from_event(row, "pickup"), payload
                    )
                    or self._find_seeded_inventory_row_for_payload_event(rows, row)
                )
                if target is None and payload:
                    for candidate in rows:
                        if (
                            str(candidate.get("payload", "")).strip() == payload
                            and row_allows_payload(candidate, payload)
                        ):
                            target = candidate
                            break
                if target is None:
                    continue
                target.update(
                    {
                        "payload": "-",
                        # Once the bin has physically left this inventory slot,
                        # clear the instance id from the slot.  Otherwise a later
                        # planning row for the return task can match the empty
                        # slot by instance id and make the bin appear to be back
                        # before the AMR has delivered it.
                        "payload_instance_id": "-",
                        "task_id": str(row.get("task_id", "")).strip() or "-",
                        "amr_id": str(row.get("amr_id", "")).strip() or "-",
                        "status": "Empty",
                        "timestamp": event.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "fill_start_time": event.start_time.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "source": "Simulation CSV",
                        "waste_volume_m3": 0.0,
                        "fill_start_volume_m3": 0.0,
                    }
                )
                target.update(self._enrich_payload_row_details(target))

        # Re-enrich every row at the current timeline position.  This is what
        # makes waste container labels/tooltips continue to fill while the
        # visualiser plays, even when there is no new CSV event on this tick.
        result = [self._enrich_payload_row_details(row) for row in rows]
        self._inventory_rows_cache[cache_key] = [dict(row) for row in result]
        self._trim_inventory_rows_cache()
        return [dict(row) for row in result]

    def _inventory_space_rows_for_location(self, location_name: str) -> List[dict]:
        location = self._location_by_name(location_name)
        if not location:
            return []

        # Use the same live occupancy reconstruction as the payload dialog, so
        # pickup/drop-off CSV events and seeded waste containers are reflected
        # in the inventory-space status table.
        payload_rows = self._inventory_payload_rows_for_location(location_name)
        payload_by_space = {
            str(row.get("space", "") or "").strip(): row for row in payload_rows
        }

        rows = []
        for idx, space in enumerate(
            location.get("inventory_spaces", []) or [], start=1
        ):
            space_name = str(space.get("name", "")).strip() or f"Inventory {idx}"
            live_row = payload_by_space.get(space_name, {})
            payload = str(live_row.get("payload", "-") or "-").strip()
            amr_id = str(space.get("amr_id", "") or "").strip()
            if amr_id and (not payload or payload == "-"):
                payload = f"AMR: {amr_id}"
            occupied = bool(payload and payload != "-")
            points = list(space.get("points", []) or [])
            rows.append(
                {
                    "name": space_name,
                    "length_m": space.get("length_m", space.get("length", "")),
                    "width_m": space.get("width_m", space.get("width", "")),
                    "height_m": space.get("height_m", space.get("height", "")),
                    "occupied": "Yes" if occupied else "No",
                    "payload": payload if occupied else "Empty",
                    "task_id": live_row.get("task_id", space.get("task_id", "-")),
                    "reserved_by_task": space.get("reserved_by_task", "-"),
                    "points": len(points),
                }
            )
        return rows

    def show_location_inventory_spaces(self, location_name: str):
        point = self.layout_model.points.get(location_name, {})
        if point.get("kind") != "location":
            QMessageBox.information(
                self,
                "Inventory spaces",
                f"{location_name} is not a location node.",
            )
            return

        rows = self._inventory_space_rows_for_location(location_name)
        if not rows:
            QMessageBox.information(
                self,
                f"Inventory status - {location_name}",
                f"Location: {location_name}\n\nNo inventory spaces are defined for this location.",
            )
            return

        dialog = LocationInventorySpacesDialog(self, location_name, rows)
        dialog.exec()

    def show_location_inventory_payloads(self, location_name: str):

        rows = self._inventory_payload_rows_for_location(location_name)
        if not rows:
            QMessageBox.information(
                self,
                f"Inventory payloads - {location_name}",
                f"Location: {location_name}\n\nNo inventory spaces are defined for this location.",
            )
            return

        dialog = LocationInventoryPayloadDialog(
            self,
            location_name,
            rows,
            self.current_time,
        )
        dialog.exec()

    def show_dropoff_tasks_at_node(self, node_name: str):
        tasks = self._tasks_dropping_off_at_node(node_name)

        if not tasks:
            QMessageBox.information(
                self,
                f"Drop-off tasks at {node_name}",
                f"Node: {node_name}\n\nNo tasks drop off at this location.",
            )
            return

        lines = [f"Node: {node_name}", f"Drop-off tasks: {len(tasks)}", ""]
        for task in tasks:
            lines.append(
                f"Task {task['id']} | Pickup: {task['pickup']} | Payload: {task['payload']} | "
                f"Release: {task['release_datetime']} | Priority: {task['priority']} | "
                f"Target: {task['target_time']} | Route profile: {task['route_profile']}"
            )

        QMessageBox.information(
            self,
            f"Drop-off tasks at {node_name}",
            "\n".join(lines),
        )

    def show_pickup_tasks_at_node(self, node_name: str):
        tasks = self._tasks_picking_up_at_node(node_name)

        if not tasks:
            QMessageBox.information(
                self,
                f"Pickup tasks at {node_name}",
                f"Node: {node_name}\n\nNo tasks pick up from this location.",
            )
            return

        lines = [f"Node: {node_name}", f"Pickup tasks: {len(tasks)}", ""]
        for task in tasks:
            lines.append(
                f"Task {task['id']} | Dropoff: {task['dropoff']} | Payload: {task['payload']} | "
                f"Release: {task['release_datetime']} | Priority: {task['priority']} | "
                f"Target: {task['target_time']} | Route profile: {task['route_profile']}"
            )

        QMessageBox.information(
            self,
            f"Pickup tasks at {node_name}",
            "\n".join(lines),
        )

    def show_node_amr_status(self, node_name: str):
        states = self._current_amrs_at_node(node_name)
        current_stamp = (
            self.current_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.current_time
            else "-"
        )

        if not states:
            QMessageBox.information(
                self,
                f"AMRs at {node_name}",
                f"Node: {node_name}\nTime: {current_stamp}\n\nNo AMRs are currently at this node.",
            )
            return

        lines = [f"Node: {node_name}", f"Time: {current_stamp}", ""]
        for state in states:
            status_text = (
                state.get("status")
                or state.get("event_type")
                or state.get("segment_type")
                or "-"
            )
            task_id = state.get("task_id") or "-"
            payload = state.get("payload") or "-"
            lines.append(
                f"{state.get('amr_id', 'AMR')} | Status: {status_text} | Task: {task_id} | Payload: {payload}"
            )

        QMessageBox.information(self, f"AMRs at {node_name}", "\n".join(lines))

    def on_view_right_click(self, event: QMouseEvent):
        node_name = self._node_name_at_view_event(event)
        if not node_name:
            return

        self.node_context_menu.clear()

        self.node_context_menu.addAction(
            "Show AMRs at node",
            lambda checked=False, name=node_name: self.show_node_amr_status(name),
        )

        self.node_context_menu.addAction(
            "Show drop-off tasks at location",
            lambda checked=False, name=node_name: self.show_dropoff_tasks_at_node(name),
        )

        self.node_context_menu.addAction(
            "Show pickup tasks at location",
            lambda checked=False, name=node_name: self.show_pickup_tasks_at_node(name),
        )

        point = self.layout_model.points.get(node_name, {})
        if point.get("kind") == "location":
            self.node_context_menu.addSeparator()
            self.node_context_menu.addAction(
                "View inventory spaces",
                lambda checked=False, name=node_name: self.show_location_inventory_spaces(
                    name
                ),
            )
            self.node_context_menu.addAction(
                "Show inventory payloads",
                lambda checked=False, name=node_name: self.show_location_inventory_payloads(
                    name
                ),
            )

        self.node_context_menu.popup(event.globalPosition().toPoint())

    def _amr_charge_state_label(self, state: dict) -> str:
        row = state.get("raw", {}) or {}
        value = row.get("battery_soc_after", row.get("battery_soc_percent", ""))
        if value in (None, ""):
            value = row.get("battery_soc_before", "")
        try:
            soc_text = f"{float(value):.0f}%"
        except Exception:
            soc_text = ""
        charging = str(row.get("is_charging", "") or "").strip().lower() in {"true", "1", "yes", "charging"}
        event_text = str(row.get("event_type", state.get("event_type", "")) or "").lower()
        segment_text = str(row.get("segment_type", state.get("segment_type", "")) or "").lower()
        status_text = str(row.get("status", state.get("status", "")) or "").lower()
        if "charge" in event_text or "charge" in segment_text or "charging" in status_text:
            charging = True
        if soc_text and charging:
            return f"Charge {soc_text}"
        if soc_text:
            return f"Battery {soc_text}"
        return "Charging" if charging else ""

    def draw_dynamic_state_qt(self, floor: int):
        if not self.current_time or not self.sim_log.events:
            if self.event_box is not None:
                self.event_box.clear()
            return

        amr_states, recent_events = self._current_state()
        followed_amr = self.follow_combo.currentText().strip()
        visible_world = self._visible_world_rect(margin_m=6.0)

        for amr_id, state in amr_states.items():
            if self._state_is_stowed_in_amr_space(state):
                # AMR bays are drawn by draw_room_payloads_qt so the bay text and
                # dynamic AMR label do not overlap at initial load or after return.
                continue
            if state.get("floor") != floor:
                continue
            if state.get("x") is None or state.get("y") is None:
                continue
            is_followed = (
                self.follow_enabled_check.isChecked() and followed_amr == amr_id
            )
            if not is_followed and not self._point_in_world_rect(
                float(state["x"]), float(state["y"]), visible_world
            ):
                continue

            x, y = self.world_to_scene(state["x"], state["y"])

            if self.show_amr_box_check.isChecked():
                self._draw_amr_box_colored_qt(
                    state, fill="#ff9f1c" if is_followed else "#4da3ff"
                )
            else:
                r = 0.5
                item = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
                item.setBrush(QBrush(QColor("#ff9f1c" if is_followed else "#4da3ff")))
                item.setPen(QPen(QColor("#858585"), 0.0))
                item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
                self.graphics_scene.addItem(item)
                self._active_dynamic_items().append(item)

            action = (
                state.get("event_type")
                or state.get("segment_type")
                or state.get("status")
                or ""
            )
            label_lines = [amr_id]
            if action:
                label_lines.append(str(action))
            if getattr(self, "show_amr_charge_state_check", None) is not None and self.show_amr_charge_state_check.isChecked():
                charge_label = self._amr_charge_state_label(state)
                if charge_label:
                    label_lines.append(charge_label)
            length, width = self._amr_dimensions_for_state(state)
            heading_deg = math.degrees(self._amr_heading_radians_for_state(state))
            self.draw_fitted_text_box_item(
                x,
                y,
                max(0.05, length * 0.92),
                max(0.05, width * 0.86),
                "\n".join(line for line in label_lines if line),
                "#ffffff",
                dynamic=True,
                rotation_deg=heading_deg,
                max_lines=3,
                min_pixel_size=3,
                max_pixel_size=None,
                scene_height=0.12,
            )

        if self.event_box is None:
            return

        self.event_box.clear()
        for item in recent_events:
            row = item["row"]
            stamp = item["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"{stamp} | "
                f"{row.get('amr_id', '')} | "
                f"{row.get('payload', '')} | "
                f"{row.get('segment_type', '')} | "
                f"{row.get('start_node', '')} -> {row.get('end_node', '')}"
            )
            self.event_box.append(line)

    def _scroll_timeline_to_time(self, value: Optional[datetime]):
        if value is None or not hasattr(self, "timeline_widget"):
            return
        self.refresh_timeline()
        x = self.timeline_widget._time_to_x(value)
        bar = self.timeline_scroll.horizontalScrollBar()
        target = int(x - (self.timeline_scroll.viewport().width() / 2))
        bar.setValue(max(0, min(bar.maximum(), target)))
        self.timeline_widget.update()

    def _clamp_to_sim_range(self, value: datetime) -> datetime:
        if self.sim_log.start_time and value < self.sim_log.start_time:
            return self.sim_log.start_time
        if self.sim_log.end_time and value > self.sim_log.end_time:
            return self.sim_log.end_time
        return value

    def skip_timeline_days(self, days: int):
        if not self.current_time:
            return
        self.current_time = self._clamp_to_sim_range(
            self.current_time + timedelta(days=int(days))
        )
        self.update_time_display()
        self.refresh_dynamic_scene()
        self._scroll_timeline_to_time(self.current_time)
        self.view.viewport().update()

    def skip_timeline_hours(self, hours: int):
        if not self.current_time:
            return
        self.current_time = self._clamp_to_sim_range(
            self.current_time + timedelta(hours=int(hours))
        )
        self.update_time_display()
        self.refresh_dynamic_scene()
        self._scroll_timeline_to_time(self.current_time)
        self.view.viewport().update()

    def jump_timeline_to_current_day_start(self):
        if not self.current_time:
            return
        day_start = datetime(
            self.current_time.year,
            self.current_time.month,
            self.current_time.day,
            tzinfo=self.current_time.tzinfo,
        )
        self.current_time = self._clamp_to_sim_range(day_start)
        self.update_time_display()
        self.refresh_dynamic_scene()
        self._scroll_timeline_to_time(self.current_time)
        self.view.viewport().update()

    def zoom_timeline(self, factor: float):
        if not hasattr(self, "timeline_widget"):
            return
        anchor = self.current_time or self.sim_log.start_time
        self.timeline_widget.zoom_by_factor(factor, anchor)

    def on_timeline_zoom_changed(self, text: str):
        if not hasattr(self, "timeline_widget"):
            return
        text = str(text or "").strip().lower()
        seconds = None
        if text == "fit":
            total_seconds = 0.0
            if self.sim_log.start_time and self.sim_log.end_time:
                total_seconds = max(
                    1.0,
                    (self.sim_log.end_time - self.sim_log.start_time).total_seconds(),
                )
            visible_width = max(
                300,
                self.timeline_scroll.viewport().width()
                - self.timeline_widget.left_pad
                - self.timeline_widget.right_pad,
            )
            seconds = (
                total_seconds / visible_width
                if total_seconds > 0
                else self.timeline_widget.default_seconds_per_pixel
            )
        else:
            mapping = {
                "15 min": 15 * 60,
                "30 min": 30 * 60,
                "1 hour": 60 * 60,
                "3 hours": 3 * 60 * 60,
                "6 hours": 6 * 60 * 60,
                "12 hours": 12 * 60 * 60,
                "1 day": 24 * 60 * 60,
            }
            window_seconds = mapping.get(text)
            if window_seconds:
                visible_width = max(
                    300,
                    self.timeline_scroll.viewport().width()
                    - self.timeline_widget.left_pad
                    - self.timeline_widget.right_pad,
                )
                seconds = window_seconds / visible_width
        if seconds is not None:
            self.timeline_widget.set_zoom_seconds_per_pixel(seconds)
            self._scroll_timeline_to_time(self.current_time or self.sim_log.start_time)

    def update_time_display(self):
        if not self.current_time:
            self.time_label.setText("No simulation loaded")
            self.update_lift_monitor_dialog()
            self.update_amr_payload_monitor_dialog()
            return
        fraction = (
            self.sim_log.time_to_fraction(self.current_time)
            if self.sim_log.start_time
            else 0.0
        )
        self.slider.blockSignals(True)
        self.slider.setValue(int(fraction * 1000))
        self.slider.blockSignals(False)
        start = (
            self.sim_log.start_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.sim_log.start_time
            else "-"
        )
        end = (
            self.sim_log.end_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.sim_log.end_time
            else "-"
        )
        self.time_label.setText(
            f"Current: {self.current_time.strftime('%Y-%m-%d %H:%M:%S')}\nStart: {start}\nEnd: {end}"
        )
        # Do not rebuild the full AMR timeline on every playback tick.  The
        # blocks only change when the CSV/layout changes; during playback only
        # the playhead needs repainting.
        if hasattr(self, "timeline_widget"):
            self.timeline_widget.current_time = self.current_time
            self.timeline_widget.update()
        self.update_lift_monitor_dialog()
        self.update_amr_payload_monitor_dialog()

    def on_slider_change(self, value):
        if not self.sim_log.start_time:
            return
        self.current_time = self.sim_log.fraction_to_time(value / 1000.0)
        self._invalidate_runtime_caches()
        self.update_time_display()
        self.refresh_dynamic_scene()
        self.view.viewport().update()

    def on_speed_changed(self, _value=None):
        try:
            self.play_speed = float(self.speed_combo.currentText())
        except Exception:
            self.play_speed = 60.0

    def toggle_play(self):
        self.is_playing = not self.is_playing
        self.play_btn.setText("Pause" if self.is_playing else "Play")
        if self.is_playing:
            self._last_play_tick_wall_time = time.monotonic()
            # Paint at a UI frame rate rather than one large 100 ms step.  The
            # simulation time advance is calculated from real elapsed time, so
            # playback speed stays stable even if a frame is delayed.
            self.play_timer.start(self._target_playback_frame_interval_ms)
        else:
            self._last_play_tick_wall_time = None
            self.play_timer.stop()

    def _tick(self):
        if not self.is_playing or not self.current_time or not self.sim_log.end_time:
            return
        now = time.monotonic()
        last = self._last_play_tick_wall_time or now
        elapsed_wall = max(0.001, min(0.25, now - last))
        self._last_play_tick_wall_time = now
        self.current_time += timedelta(seconds=self.play_speed * elapsed_wall)
        if self.current_time >= self.sim_log.end_time:
            self.current_time = self.sim_log.end_time
            self.is_playing = False
            self.play_btn.setText("Play")
            self._last_play_tick_wall_time = None
            self.play_timer.stop()
        self.update_time_display()
        self.refresh_dynamic_scene()
        self.view.viewport().update()

    def step_seconds(self, seconds: int):
        if not self.current_time:
            return
        self.current_time += timedelta(seconds=seconds)
        if self.sim_log.start_time and self.current_time < self.sim_log.start_time:
            self.current_time = self.sim_log.start_time
        if self.sim_log.end_time and self.current_time > self.sim_log.end_time:
            self.current_time = self.sim_log.end_time
        self._invalidate_runtime_caches()
        self.update_time_display()
        self.refresh_dynamic_scene()
        self.view.viewport().update()

    def jump_start(self):
        if self.sim_log.start_time:
            self.current_time = self.sim_log.start_time
            self._invalidate_runtime_caches()
            self.update_time_display()
            self.refresh_dynamic_scene()
            self.view.viewport().update()

    def jump_end(self):
        if self.sim_log.end_time:
            self.current_time = self.sim_log.end_time
            self._invalidate_runtime_caches()
            self.update_time_display()
            self.refresh_dynamic_scene()
            self.view.viewport().update()

    def jump_first_travel(self):
        travel_time = self.sim_log.first_travel_time()
        if travel_time is not None:
            self.current_time = travel_time
            self._invalidate_runtime_caches()
            self.update_time_display()
            self.refresh_dynamic_scene()
            self.view.viewport().update()

    def update_follow_amr_options(self):
        amr_ids = sorted(
            {
                (event.row.get("amr_id") or "").strip()
                for event in self.sim_log.events
                if (event.row.get("amr_id") or "").strip()
            }
        )
        self.follow_combo.blockSignals(True)
        self.follow_combo.clear()
        self.follow_combo.addItems(amr_ids)
        self.follow_combo.blockSignals(False)

    def load_floor_dxfs_from_json(self):
        # Backwards-compatible entry point retained for older callers.
        self.start_loading_floor_dxfs_from_json()

    def _finish_first_json_load(self):
        self.refresh_static_scene()
        self.refresh_dynamic_scene()
        self.refresh_timeline()
        self.fit_view()
        self.view.viewport().update()

    def load_json_file(self, path: str) -> bool:
        try:
            self.layout_model.load(path)
        except json.JSONDecodeError as exc:
            QMessageBox.critical(
                self,
                "Could not load layout JSON",
                f"{Path(path).name} is not valid JSON.\n\n{exc}",
            )
            self.set_status("Layout JSON load failed: invalid JSON syntax")
            return False
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not load layout JSON",
                f"{Path(path).name} could not be read.\n\n{exc}",
            )
            self.set_status("Layout JSON load failed: file could not be read")
            return False
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not load layout JSON",
                f"{Path(path).name} could not be loaded.\n\n{exc}",
            )
            self.set_status("Layout JSON load failed")
            return False

        self.current_json_path = path
        self._invalidate_runtime_caches()

        floors = self.layout_model.floors()
        if floors:
            self.floor_spin.blockSignals(True)
            self.floor_spin.setValue(int(floors[0]))
            self.floor_spin.blockSignals(False)

        self.update_loaded_files()
        self._sync_timeline_from_layout_and_csv()
        self.on_timeline_zoom_changed(self.timeline_zoom_combo.currentText())

        # Build initial scene contents immediately
        self.refresh_static_scene()
        self.refresh_dynamic_scene()
        self.refresh_timeline()

        # Let Qt finish sizing/layout before fitting the scene
        QTimer.singleShot(0, self._finish_first_json_load)

        self.start_loading_floor_dxfs_from_json()
        self.set_status(f"Loaded layout {Path(path).name}")
        return True

    def open_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Layout JSON", "", "JSON files (*.json)"
        )
        if not path:
            return
        self.load_json_file(path)

    def open_dxf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open DXF", "", "DXF files (*.dxf)")
        if not path:
            return

        floor = self.current_floor()
        self.dxf_paths_by_floor[floor] = path
        self.dxf_loading_failures.pop(floor, None)

        old_items = self.dxf_items_by_floor.pop(floor, [])
        for item in old_items:
            self.graphics_scene.removeItem(item)

        self.dxf_scenes.pop(floor, None)
        self.current_dxf_floor = None
        self.update_loaded_files()
        self.set_status(f"Mapped DXF {Path(path).name} to floor {floor}")

        self._start_dxf_load_batch(
            [{"floor": floor, "filepath": path}],
            label=f"Loading DXF for floor {floor}...",
        )

    def load_csv_file(self, path: str) -> bool:
        try:
            self.sim_log.load(path)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not load simulation CSV",
                f"{Path(path).name} could not be read.\n\n{exc}",
            )
            self.set_status("Simulation CSV load failed: file could not be read")
            return False
        except csv.Error as exc:
            QMessageBox.critical(
                self,
                "Could not load simulation CSV",
                f"{Path(path).name} is not a valid CSV file.\n\n{exc}",
            )
            self.set_status("Simulation CSV load failed: invalid CSV")
            return False
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not load simulation CSV",
                f"{Path(path).name} could not be loaded.\n\n{exc}",
            )
            self.set_status("Simulation CSV load failed")
            return False

        self.current_csv_path = path
        self._invalidate_runtime_caches()
        self.update_follow_amr_options()
        if not self.sim_log.events:
            QMessageBox.critical(
                self, "No events", "No timestamped rows were found in the CSV."
            )
            self.current_csv_path = None
            self.update_loaded_files()
            self.set_status("Simulation CSV load failed: no timestamped event rows")
            return False
        self.update_loaded_files()
        self._sync_timeline_from_layout_and_csv()
        self.on_timeline_zoom_changed(self.timeline_zoom_combo.currentText())
        self.refresh_all()
        self.set_status(
            f"Loaded simulation CSV {Path(path).name} with {len(self.sim_log.events)} events"
        )
        return True

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Simulation CSV", "", "CSV files (*.csv)"
        )
        if not path:
            return
        self.load_csv_file(path)

    def _content_bounds(self):
        floor = self.current_floor()
        dxf_scene = self.dxf_scenes.get(floor)
        if dxf_scene and dxf_scene.bounds:
            return dxf_scene.bounds

        floor_points = self.layout_model.points_for_floor(floor)
        if floor_points:
            xs = [float(p["x"]) for p in floor_points.values()]
            ys = [float(p["y"]) for p in floor_points.values()]
            return min(xs), min(ys), max(xs), max(ys)

        return None

    def set_floor(self, floor: int):
        if floor == self.current_floor():
            self.refresh_all()
            return

        self.floor_spin.blockSignals(True)
        self.floor_spin.setValue(int(floor))
        self.floor_spin.blockSignals(False)

        self.refresh_all()

    def fit_view(self):
        bounds = self._content_bounds()
        if not bounds:
            return

        min_x, min_y, max_x, max_y = bounds

        rect_left = min_x
        rect_top = -max_y
        rect_width = max(max_x - min_x, 1.0)
        rect_height = max(max_y - min_y, 1.0)

        content_rect = QRectF(rect_left, rect_top, rect_width, rect_height)

        self.view.resetTransform()
        self.view.fitInView(content_rect, Qt.KeepAspectRatio)

        pad = max(rect_width, rect_height, 1000.0) * 20.0
        self.graphics_scene.setSceneRect(content_rect.adjusted(-pad, -pad, pad, pad))

        self.refresh_all()

    def _sync_timeline_from_layout_and_csv(self):
        layout_start = self.layout_model.task_start_time
        layout_end = self.layout_model.task_end_time
        csv_start = self.sim_log.start_time
        csv_end = self.sim_log.end_time
        sim_start = self.layout_model.simulation_start_time
        sim_end = self.layout_model.simulation_end_time

        # The visual timeline should represent the configured simulation window,
        # not just the first/last CSV event or static task.  Generated tasks and
        # idle periods are then shown in their correct position within the full
        # sim horizon.  Fall back to CSV/layout times only when the JSON does not
        # provide simulation start/end.
        start_candidates = [x for x in (csv_start, layout_start) if x is not None]
        end_candidates = [x for x in (csv_end, layout_end) if x is not None]

        self.sim_log.start_time = sim_start or (
            min(start_candidates) if start_candidates else None
        )
        self.sim_log.end_time = sim_end or (
            max(end_candidates) if end_candidates else self.sim_log.start_time
        )

        if self.current_time is None:
            self.current_time = self.sim_log.start_time
        elif self.sim_log.start_time and self.current_time < self.sim_log.start_time:
            self.current_time = self.sim_log.start_time
        elif self.sim_log.end_time and self.current_time > self.sim_log.end_time:
            self.current_time = self.sim_log.end_time

        # When the data range changes, keep the first events reachable.
        if hasattr(self, "timeline_scroll"):
            self.timeline_scroll.horizontalScrollBar().setValue(0)

        self.update_time_display()

    def _follow_overlay_lines(self):
        if not self.follow_enabled_check.isChecked():
            return None

        followed_amr = self.follow_combo.currentText().strip()
        if not followed_amr or not self.current_time or not self.sim_log.events:
            return None

        amr_states, _recent_events = self.sim_log.state_at(
            self.current_time, self.layout_model
        )
        state = amr_states.get(followed_amr)
        if not state:
            return None

        task_id = state.get("task_id") or "-"
        onboard_label = self._format_onboard_payloads_for_label(state.get("raw", {}))
        payload = onboard_label or state.get("payload") or "-"
        start_pos = state.get("from_location") or state.get("start_node") or "-"
        end_pos = state.get("to_location") or state.get("end_node") or "-"
        start_time = (
            state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
            if state.get("start_time")
            else "-"
        )
        duration = SimulationLog._format_runtime(
            float(state.get("task_runtime_sec", 0.0))
        )

        return [
            f"Follow AMR: {followed_amr}",
            f"Task ID: {task_id}",
            f"Payload: {payload}",
            f"Start: {start_pos}",
            f"Finish: {end_pos}",
            f"Start time: {start_time}",
            f"Current duration: {duration}",
        ]

    def _draw_overlay_box(self, painter, x, y, w, lines, border_color, title_color):
        margin_x = 10
        margin_y = 8
        line_h = 18
        box_h = (margin_y * 2) + (len(lines) * line_h)

        painter.save()

        painter.setPen(QPen(QColor(border_color), 1))
        painter.setBrush(QBrush(QColor("#151515")))
        painter.drawRect(QRect(x, y, w, box_h))

        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)

        for i, line in enumerate(lines):
            painter.setPen(QColor(title_color if i == 0 else "white"))
            painter.drawText(x + margin_x, y + margin_y + 12 + (i * line_h), line)

        painter.restore()

    def draw_overlay_panels(self, painter, viewport_rect):
        self._draw_text_overlay_records(painter, viewport_rect)

        legend_lines = [
            "Legend",
            "Green circle = location",
            "Yellow square = corridor node",
            "Red diamond = lift node",
            "Blue AMR = active AMR, orange = followed AMR",
            "Green rectangles = payloads in room spaces",
            "Purple rectangles = seeded waste containers",
            "Timeline:",
            "blue=move, orange=lift, ",
            "green=charge, purple=pickup/dropoff",
            f"Floor: {self.current_floor()}",
        ]
        self._draw_overlay_box(
            painter,
            12,
            12,
            320,
            legend_lines,
            "#333333",
            "white",
        )

        follow_lines = self._follow_overlay_lines()
        if follow_lines:
            self._draw_overlay_box(
                painter,
                viewport_rect.width() - 332,
                12,
                320,
                follow_lines,
                "#ff9f1c",
                "#ffe2b3",
            )

    def update_follow_view(self):
        if not self.follow_enabled_check.isChecked():
            return
        if not self.current_time or not self.sim_log.events:
            return

        followed_amr = self.follow_combo.currentText().strip()
        if not followed_amr:
            return

        amr_states, _ = self._current_state()
        state = amr_states.get(followed_amr)
        if not state:
            return
        if state.get("x") is None or state.get("y") is None:
            return

        amr_floor = state.get("floor")
        if amr_floor is None:
            return

        if int(amr_floor) != self.current_floor():
            self.set_floor(int(amr_floor))

        sx, sy = self.world_to_scene(state["x"], state["y"])
        self.view.centerOn(sx, sy)
        self.view.viewport().update()

    def _format_duration_label(self, start_time: datetime, end_time: datetime) -> str:
        seconds = max(0.0, (end_time - start_time).total_seconds())
        total = int(seconds)
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _sum_task_segment_seconds(self, segments: List[dict]) -> float:
        total = 0.0

        for segment in segments:
            label = (segment.get("label") or "").strip().lower()
            event_type = (segment.get("event_type") or "").strip().lower()
            segment_type = (segment.get("segment_type") or "").strip().lower()

            # Ignore bookkeeping rows that should not inflate task duration.
            if label in {"task assigned", "task complete", "task overrun"}:
                continue
            if event_type in {
                "task_assigned",
                "task assigned",
                "task_complete",
                "task complete",
                "task_completed",
                "task completed",
                "task_overrun",
                "task overrun",
            }:
                continue

            start_dt = segment.get("start_time")
            end_dt = segment.get("end_time")

            if start_dt is None or end_dt is None:
                continue

            seconds = max(0.0, (end_dt - start_dt).total_seconds())
            total += seconds

        return total

    def _configured_location_names(self) -> set:
        return {
            str(location.get("name", "") or "").strip()
            for location in self.layout_model.data.get("locations", []) or []
            if isinstance(location, dict)
            and str(location.get("name", "") or "").strip()
        }

    def _department_name_lookup(self) -> Dict[str, str]:
        """Return aliases for department id/name -> configured department name.

        The task browser should show the human-readable department name.  Some
        CSV rows only carry department_id, so keep id aliases but always resolve
        them to the configured name where possible.
        """
        lookup: Dict[str, str] = {}
        for department in self.layout_model.data.get("departments", []) or []:
            if not isinstance(department, dict):
                continue
            dept_id = str(department.get("id", "") or "").strip()
            dept_name = str(department.get("name", "") or "").strip()
            display_name = dept_name or dept_id
            if not display_name:
                continue
            if dept_id:
                lookup[dept_id] = display_name
            if dept_name:
                lookup[dept_name] = display_name
        return lookup

    def _department_display_name(
        self, value: str, department_names: Optional[Dict[str, str]] = None
    ) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if department_names is None:
            department_names = self._department_name_lookup()
        return department_names.get(value, value)

    def _location_department_lookup(self) -> Dict[str, str]:
        lookup: Dict[str, str] = {}
        valid_locations = self._configured_location_names()
        department_names = self._department_name_lookup()

        # First use the explicit location fields, resolving IDs to configured
        # names.  Do not invent departments from corridor node names.
        for location in self.layout_model.data.get("locations", []) or []:
            if not isinstance(location, dict):
                continue
            name = str(location.get("name", "") or "").strip()
            if not name or name not in valid_locations:
                continue
            raw_dept_name = str(location.get("department_name", "") or "").strip()
            raw_dept = (
                raw_dept_name
                or str(location.get("department", "") or "").strip()
                or str(location.get("department_id", "") or "").strip()
            )
            dept = self._department_display_name(raw_dept, department_names)
            if dept:
                lookup[name] = dept

        # Then fill missing locations from department assignments.  Use the
        # department name, never the ID, for display/grouping.
        for department in self.layout_model.data.get("departments", []) or []:
            if not isinstance(department, dict):
                continue
            dept_id = str(department.get("id", "") or "").strip()
            dept_name = str(department.get("name", "") or "").strip()
            display_name = dept_name or dept_id
            if not display_name:
                continue

            for key in ("locations", "location_names", "department_locations"):
                raw = department.get(key, []) or []
                if isinstance(raw, str):
                    raw = [raw]
                if isinstance(raw, list):
                    for loc in raw:
                        loc_name = str(loc or "").strip()
                        if loc_name in valid_locations:
                            lookup.setdefault(loc_name, display_name)

            tg_locations = department.get("task_generation_locations", {}) or {}
            if isinstance(tg_locations, dict):
                for entry in tg_locations.values():
                    values = []
                    if isinstance(entry, dict):
                        values.extend(entry.get("pickup_dropoff_locations", []) or [])
                        values.extend(entry.get("locations", []) or [])
                    elif isinstance(entry, list):
                        values.extend(entry)
                    for loc in values:
                        loc_name = str(loc or "").strip()
                        if loc_name in valid_locations:
                            lookup.setdefault(loc_name, display_name)
        return lookup

    def _valid_task_location_from_row(
        self, row: dict, keys: Tuple[str, ...], valid_locations: set
    ) -> str:
        for key in keys:
            value = str(row.get(key, "") or "").strip()
            if value in valid_locations:
                return value
        return ""

    def _infer_department_for_task_row(
        self, row: dict, location_departments: Dict[str, str]
    ) -> str:
        department_names = self._department_name_lookup()

        # Prefer a proper display name if the CSV provides one.  If only an ID is
        # present, resolve it through the JSON department list.
        explicit_name = str(
            row.get("department", "") or row.get("department_name", "") or ""
        ).strip()
        if explicit_name:
            return self._department_display_name(explicit_name, department_names)

        explicit_id = str(row.get("department_id", "") or "").strip()
        if explicit_id:
            resolved = self._department_display_name(explicit_id, department_names)
            if resolved and resolved != explicit_id:
                return resolved

        # Infer from real configured location assignments only.  Do not parse a
        # D1 prefix from arbitrary node names because that made corridor/lift
        # nodes appear as departments.
        for key in (
            "dropoff",
            "to_location",
            "finish_location",
            "end_location",
            "pickup",
            "from_location",
        ):
            loc = str(row.get(key, "") or "").strip()
            if loc in location_departments:
                return location_departments[loc]

        return "Unassigned department"

    def _task_datetime_display(self, dt: Optional[datetime], fallback: str = "") -> str:
        if dt is not None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return str(fallback or "-").strip() or "-"

    def _full_task_row_duration(
        self, start_dt: Optional[datetime], end_dt: Optional[datetime]
    ) -> str:
        if start_dt is None or end_dt is None or end_dt < start_dt:
            return "-"
        return SimulationLog._format_runtime((end_dt - start_dt).total_seconds())

    def _csv_full_task_rows(self, location_departments: Dict[str, str]) -> List[dict]:
        valid_locations = self._configured_location_names()
        tasks: Dict[str, dict] = {}

        for event in self.sim_log.events or []:
            row = event.row
            task_id = str(row.get("task_id", "") or "").strip()
            if not task_id:
                continue
            if (
                not str(row.get("amr_id", "") or "").strip()
                and not str(row.get("person_id", "") or "").strip()
            ):
                # Generated/planning rows can describe future tasks but are not a
                # full executed task journey.
                continue

            event_type = str(row.get("event_type", "") or "").strip().lower()
            segment_type = str(row.get("segment_type", "") or "").strip().lower()
            status = str(row.get("status", "") or "").strip().lower()
            text = " ".join([event_type, segment_type, status])

            start_loc = self._valid_task_location_from_row(
                row,
                ("pickup", "from_location", "start_location"),
                valid_locations,
            )
            finish_loc = self._valid_task_location_from_row(
                row,
                ("dropoff", "to_location", "finish_location", "end_location"),
                valid_locations,
            )

            task = tasks.setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "payload": "-",
                    "start_time": event.start_time,
                    "end_time": event.end_time,
                    "start_location": "",
                    "finish_location": "",
                    "department": "",
                    "department_id": "",
                    "staff": "",
                    "people_required": "",
                    "status": "in progress",
                    "source": "CSV",
                    "details": "",
                },
            )

            if event.start_time < task["start_time"]:
                task["start_time"] = event.start_time
            if event.end_time and event.end_time > task["end_time"]:
                task["end_time"] = event.end_time

            payload = str(
                row.get("payload", "") or row.get("container_type", "") or ""
            ).strip()
            if payload and task.get("payload") in {"", "-"}:
                task["payload"] = payload

            if not task.get("start_location"):
                if "pickup" in text or "pick_up" in text or "load" in text:
                    task["start_location"] = start_loc
                elif start_loc:
                    task["start_location"] = start_loc
            if finish_loc:
                if any(
                    token in text
                    for token in (
                        "dropoff",
                        "drop_off",
                        "deliver",
                        "unload",
                        "complete",
                    )
                ):
                    task["finish_location"] = finish_loc
                elif not task.get("finish_location"):
                    task["finish_location"] = finish_loc

            dept_id = str(row.get("department_id", "") or "").strip()
            dept_name = str(row.get("department_name", "") or "").strip()
            if dept_id and not task.get("department_id"):
                task["department_id"] = dept_id
            if dept_name and not task.get("department"):
                task["department"] = dept_name

            person_id = str(row.get("person_id", "") or "").strip()
            person_resource = str(row.get("person_resource", "") or "").strip()
            people_required = str(row.get("people_required", "") or "").strip()
            if person_id and not task.get("staff"):
                task["staff"] = person_id
            elif person_resource and not task.get("staff"):
                task["staff"] = person_resource
            if people_required and people_required not in {"0", "0.0"}:
                task["people_required"] = people_required

            if (
                event_type
                in {
                    "task_complete",
                    "task_completed",
                    "task complete",
                    "task completed",
                }
                or "complete" in text
            ):
                task["status"] = "completed"
            elif "failed" in text:
                task["status"] = "failed"
            elif status:
                task["status"] = status

            detail = str(row.get("details", "") or "").strip()
            if detail:
                task["details"] = detail

        rows = []
        for task in tasks.values():
            if not task.get("start_location") and not task.get("finish_location"):
                continue
            if not task.get("department"):
                task["department"] = self._infer_department_for_task_row(
                    task, location_departments
                )
            start_dt = task.get("start_time")
            end_dt = task.get("end_time")
            task["start_time_display"] = self._task_datetime_display(start_dt)
            task["end_time_display"] = self._task_datetime_display(end_dt)
            task["start_sort_time"] = start_dt.isoformat() if start_dt else ""
            task["duration"] = self._full_task_row_duration(start_dt, end_dt)
            task["start_location"] = task.get("start_location") or "-"
            task["finish_location"] = task.get("finish_location") or "-"
            task["staff"] = task.get("staff") or "-"
            task["people_required"] = task.get("people_required") or "-"
            rows.append(task)
        return rows

    def _json_full_task_rows(self, location_departments: Dict[str, str]) -> List[dict]:
        valid_locations = self._configured_location_names()
        rows: List[dict] = []
        for task in self.layout_model.data.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", "") or "").strip() or "-"
            pickup = str(task.get("pickup", "") or "").strip()
            dropoff = str(task.get("dropoff", "") or "").strip()
            if pickup not in valid_locations and dropoff not in valid_locations:
                continue
            payload = str(task.get("payload", "") or "").strip() or "-"
            release_text = str(task.get("release_datetime", "") or "").strip()
            release_dt = LayoutModel._parse_datetime(release_text)
            row = {
                "task_id": task_id,
                "payload": payload,
                "start_time": release_dt,
                "end_time": None,
                "start_time_display": self._task_datetime_display(
                    release_dt, release_text
                ),
                "end_time_display": "-",
                "start_sort_time": (
                    release_dt.isoformat() if release_dt else release_text
                ),
                "start_location": pickup if pickup in valid_locations else "-",
                "finish_location": dropoff if dropoff in valid_locations else "-",
                "department": self._infer_department_for_task_row(
                    task, location_departments
                ),
                "department_id": str(task.get("department_id", "") or "").strip(),
                "staff": "-",
                "people_required": "-",
                "duration": "-",
                "source": "JSON",
                "status": "planned",
                "details": json.dumps(task, default=str),
            }
            rows.append(row)
        return rows

    def build_tasks_by_location_department_rows(self) -> List[dict]:
        location_departments = self._location_department_lookup()
        rows = self._csv_full_task_rows(location_departments)
        if not rows:
            rows = self._json_full_task_rows(location_departments)
        rows.sort(
            key=lambda r: (str(r.get("start_sort_time", "")), str(r.get("task_id", "")))
        )
        return rows

    def open_tasks_by_location_department_dialog(self):
        if not self.layout_model.data and not self.sim_log.events:
            QMessageBox.information(
                self,
                "No data loaded",
                "Load a layout JSON or simulation CSV first.",
            )
            return

        rows = self.build_tasks_by_location_department_rows()
        if not rows:
            QMessageBox.information(
                self,
                "No tasks found",
                "No full task records were found for configured locations/departments.",
            )
            return

        dialog = TasksByLocationDepartmentDialog(self, rows)
        if dialog.exec() != QDialog.Accepted:
            return

        if dialog.selected_time is not None:
            self.current_time = dialog.selected_time
            self.update_time_display()
            self.refresh_dynamic_scene()
            self._scroll_timeline_to_time(self.current_time)

        selected_location = str(getattr(dialog, "selected_location", "") or "").strip()
        if selected_location and selected_location in self.layout_model.points:
            point = self.layout_model.points[selected_location]
            try:
                self.set_floor(int(point.get("floor", self.current_floor())))
                sx, sy = self.world_to_scene(point.get("x", 0.0), point.get("y", 0.0))
                self.view.centerOn(sx, sy)
            except Exception:
                pass

        selected_task_id = str(getattr(dialog, "selected_task_id", "") or "").strip()
        if selected_task_id:
            self.set_status(f"Jumped to task {selected_task_id}")
        self.view.viewport().update()

    def build_task_jump_index(self) -> Dict[str, List[dict]]:
        grouped: Dict[str, List[dict]] = {}

        # Track the currently open displayed task per AMR/task_id
        open_tasks: Dict[tuple[str, str], dict] = {}

        for event in self.sim_log.events:
            row = event.row
            amr_id = (row.get("amr_id") or "").strip()
            task_id = (row.get("task_id") or "").strip()

            if not amr_id or not task_id:
                continue

            start_dt = event.start_time
            end_dt = (
                event.end_time
                if event.end_time >= event.start_time
                else event.start_time
            )

            sim_dt_str = (row.get("sim_datetime") or "").strip()
            sim_dt = (
                self.sim_log._parse_datetime(sim_dt_str) if sim_dt_str else start_dt
            )

            origin = (
                (row.get("from_location") or "").strip()
                or (row.get("start_node") or "").strip()
                or "-"
            )
            destination = (
                (row.get("to_location") or "").strip()
                or (row.get("end_node") or "").strip()
                or "-"
            )
            payload = (row.get("payload") or "").strip() or "-"

            event_type = (row.get("event_type") or "").strip()
            segment_type = (row.get("segment_type") or "").strip()
            status = (row.get("status") or "").strip()

            event_type_lower = event_type.lower()
            label_source = event_type or segment_type or status or "Segment"
            segment_label = label_source.replace("_", " ").title()

            amr_tasks = grouped.setdefault(amr_id, [])
            key = (amr_id, task_id)

            current_bucket = open_tasks.get(key)

            # Start a new displayed task only if none is currently open
            if current_bucket is None:
                current_bucket = {
                    "task_id": task_id,
                    "payload": payload,
                    "origin": origin,
                    "destination": destination,
                    "start_time": start_dt,
                    "end_time": end_dt,
                    "sim_datetime": sim_dt,
                    "segments": [],
                }
                amr_tasks.append(current_bucket)
                open_tasks[key] = current_bucket
            else:
                if start_dt < current_bucket["start_time"]:
                    current_bucket["start_time"] = start_dt
                    current_bucket["origin"] = origin
                if end_dt > current_bucket["end_time"]:
                    current_bucket["end_time"] = end_dt
                    current_bucket["destination"] = destination
                if sim_dt < current_bucket.get("sim_datetime", sim_dt):
                    current_bucket["sim_datetime"] = sim_dt
                if current_bucket["payload"] == "-" and payload != "-":
                    current_bucket["payload"] = payload

            current_bucket["segments"].append(
                {
                    "label": segment_label,
                    "origin": origin,
                    "destination": destination,
                    "start_time": start_dt,
                    "end_time": end_dt,
                    "duration": self._format_duration_label(start_dt, end_dt),
                    "event_type": event_type,
                    "segment_type": segment_type,
                    "sim_datetime": sim_dt,
                }
            )

            # Close the displayed task only on completion
            if event_type_lower in {
                "task_complete",
                "task complete",
                "task_completed",
                "task completed",
            }:
                open_tasks.pop(key, None)

        result: Dict[str, List[dict]] = {}

        for amr_id in sorted(grouped.keys()):
            task_list = grouped[amr_id]

            for task in task_list:
                task["segments"].sort(
                    key=lambda item: (
                        item.get("sim_datetime") or item["start_time"],
                        item["start_time"],
                        item["end_time"],
                        item.get("event_type", ""),
                        item.get("segment_type", ""),
                        item["label"],
                    )
                )
                total_seconds = self._sum_task_segment_seconds(task["segments"])
                task["duration"] = SimulationLog._format_runtime(total_seconds)

            task_list.sort(
                key=lambda item: (
                    item.get("sim_datetime") or item["start_time"],
                    item["start_time"],
                    item["task_id"],
                )
            )
            result[amr_id] = task_list

        return result

    def open_task_jump_dialog(self):
        if not self.sim_log.events:
            QMessageBox.information(
                self,
                "No simulation loaded",
                "Load a simulation CSV first.",
            )
            return

        grouped_tasks = self.build_task_jump_index()
        if not grouped_tasks:
            QMessageBox.information(
                self,
                "No tasks found",
                "No task rows with AMR IDs were found in the simulation log.",
            )
            return

        dialog = TaskJumpDialog(self, grouped_tasks)
        if dialog.exec() != QDialog.Accepted:
            return

        if dialog.selected_start_time is None:
            return

        self.current_time = dialog.selected_start_time
        if dialog.selected_amr_id:
            index = self.follow_combo.findText(dialog.selected_amr_id)
            if index >= 0:
                self.follow_combo.setCurrentIndex(index)
            self.follow_enabled_check.setChecked(True)

        self.update_time_display()
        self.refresh_dynamic_scene()
        self.set_status(
            f"Jumped to task start {dialog.selected_start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.view.viewport().update()

    def build_amr_timeline_data(self) -> List[dict]:
        lanes: Dict[str, List[dict]] = {}

        for event in self.sim_log.events:
            row = event.row
            amr_id = (row.get("amr_id") or "").strip()
            if not amr_id:
                continue

            start_dt = event.start_time
            end_dt = (
                event.end_time
                if event.end_time >= event.start_time
                else event.start_time
            )

            segment_type = (row.get("segment_type") or "").strip().lower()
            event_type = (row.get("event_type") or "").strip().lower()
            lift_id = (row.get("lift_id") or "").strip()

            block_type = "other"
            color = "#6f6f6f"

            if "charge" in segment_type or "charge" in event_type:
                block_type = "charging"
                color = "#2ecc71"
            elif lift_id or "lift" in segment_type or "lift" in event_type:
                block_type = "lift"
                color = "#f39c12"
            elif any(
                word in segment_type for word in ["corridor", "move", "travel"]
            ) or any(word in event_type for word in ["move", "travel"]):
                block_type = "movement"
                color = "#3498db"
            elif "pickup" in segment_type or "dropoff" in segment_type:
                block_type = "handling"
                color = "#9b59b6"

            task_id = (row.get("task_id") or "").strip()
            payload = (row.get("payload") or "").strip()
            label_parts = [task_id or block_type.title()]
            if payload:
                label_parts.append(payload)

            lanes.setdefault(amr_id, []).append(
                {
                    "start": start_dt,
                    "end": end_dt,
                    "type": block_type,
                    "color": color,
                    "label": " | ".join(label_parts),
                }
            )

        result = []
        for amr_id in sorted(lanes.keys()):
            blocks = sorted(lanes[amr_id], key=lambda b: (b["start"], b["end"]))
            merged = []

            for block in blocks:
                if not merged:
                    merged.append(block.copy())
                    continue

                prev = merged[-1]
                same_type = (
                    prev["type"] == block["type"] and prev["color"] == block["color"]
                )
                touching = block["start"] <= prev["end"]

                if same_type and touching:
                    if block["end"] > prev["end"]:
                        prev["end"] = block["end"]
                else:
                    merged.append(block.copy())

            result.append(
                {
                    "amr_id": amr_id,
                    "blocks": merged,
                }
            )

        return result

    def _timeline_display_range(self, timeline_data: List[dict]):
        starts = []
        ends = []

        if self.sim_log.start_time is not None:
            starts.append(self.sim_log.start_time)
        if self.sim_log.end_time is not None:
            ends.append(self.sim_log.end_time)

        # Guard against stale start/end values if a JSON file is loaded after a
        # CSV.  The painted range must always include the actual event blocks.
        for lane in timeline_data or []:
            for block in lane.get("blocks", []):
                if block.get("start") is not None:
                    starts.append(block["start"])
                if block.get("end") is not None:
                    ends.append(block["end"])

        # Honour the configured simulation window exactly when present.
        if self.layout_model.simulation_start_time is not None:
            starts = [self.layout_model.simulation_start_time]
        if self.layout_model.simulation_end_time is not None:
            ends = [self.layout_model.simulation_end_time]

        start_time = min(starts) if starts else None
        end_time = max(ends) if ends else start_time
        return start_time, end_time

    def refresh_timeline(self):
        if not hasattr(self, "timeline_widget"):
            return

        timeline_data = self.build_amr_timeline_data() if self.sim_log.events else []
        start_time, end_time = self._timeline_display_range(timeline_data)
        self.timeline_widget.set_data(
            timeline_data,
            start_time,
            end_time,
            self.current_time,
        )

    def on_timeline_seek(self, new_time: datetime):
        self.current_time = new_time
        self.update_time_display()
        self.refresh_dynamic_scene()
        self.refresh_timeline()
        self.view.viewport().update()


def configure_application_font(app: QApplication) -> None:
    """Force a modern Windows-safe UI font before widgets are created.

    Some Windows/DirectWrite/OpenGL combinations fall back to the legacy
    ``MS Sans Serif`` bitmap font and log:
    ``CreateFontFaceFromHDC() failed``.  Using Segoe UI keeps Qt on a
    TrueType/OpenType font path and avoids the noisy DirectWrite warning.
    """
    preferred_families = ["Arial", "Segoe UI", "Tahoma"]
    available = set(QFontDatabase.families())
    family = next((name for name in preferred_families if name in available), "Arial")
    font = QFont(family, 9)
    app.setFont(font)


def _parse_visualiser_args(argv: List[str]):
    parser = argparse.ArgumentParser(description="AMR simulation visualiser")
    parser.add_argument(
        "--config",
        help="Open the selected AMR simulator layout JSON after startup.",
    )
    parser.add_argument(
        "--csv",
        help="Open the selected AMR simulation event CSV after startup.",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_visualiser_args(sys.argv[1:] if argv is None else argv)
    if QOpenGLWidget is not None:
        default_format = QSurfaceFormat()
        default_format.setDepthBufferSize(24)
        default_format.setStencilBufferSize(8)
        default_format.setSamples(4)
        default_format.setSwapInterval(1)
        QSurfaceFormat.setDefaultFormat(default_format)
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)
    configure_application_font(app)
    window = SimulationVisualizer()
    window.show()

    def load_startup_files():
        config_loaded = True
        if args.config:
            config_loaded = window.load_json_file(args.config)
        if args.csv and config_loaded:
            window.load_csv_file(args.csv)

    if args.config or args.csv:
        QTimer.singleShot(0, load_startup_files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
