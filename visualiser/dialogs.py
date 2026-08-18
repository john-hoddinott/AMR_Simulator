import csv
import json
import math
from typing import Any, List, Optional

from advanced_dialogs import MultiSelectPicker
from models import (
    normalise_delivery_resource_policy,
    normalise_staff_delivery_resource,
)

from PySide6.QtCore import Qt, QPointF, QRectF, QTime, QDateTime
from PySide6.QtGui import QColor, QBrush, QPen, QPolygonF, QPainter, QPainterPath, QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QDoubleSpinBox,
    QGroupBox,
    QFrame,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsPolygonItem,
    QGraphicsSimpleTextItem,
    QGraphicsItem,
    QMenu,
    QGraphicsPathItem,
    QTimeEdit,
    QDateTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QFileDialog,
)


from ui_theme import polish_dialog as _polish_dialog

STAFF_MOVEMENT_POLICIES = [
    ("First available", "available_first"),
    ("Batch same location", "batch_same_location"),
    ("Prefer same location", "minimise_movement"),
]

STAFF_SHIFT_PATTERNS = [
    ("Global fixed working hours", "none"),
    ("Global 4 on / 4 off, 12-hour days", "four_on_four_off_12h"),
]

DELIVERY_RESOURCE_OPTIONS = [
    ("AMR only", "amr"),
    ("Staff only", "staff"),
    ("AMR or staff", "either"),
]


def _make_delivery_resource_combo(value=None) -> QComboBox:
    combo = QComboBox()
    for label, mode in DELIVERY_RESOURCE_OPTIONS:
        combo.addItem(label, mode)
    mode = normalise_delivery_resource_policy(value).get("mode", "amr")
    combo.setCurrentIndex(max(0, combo.findData(mode)))
    combo.setToolTip(
        "Choose which resource class may transport this logistics flow. "
        "This does not replace optional endpoint handling staff."
    )
    return combo


def _normalise_staff_movement_policy_value(value) -> str:
    policy = str(value or "").strip().lower()
    if policy == "minimize_movement":
        policy = "minimise_movement"
    valid = {item[1] for item in STAFF_MOVEMENT_POLICIES}
    return policy if policy in valid else "batch_same_location"


def _normalise_staff_shift_pattern_value(value) -> str:
    pattern = str(value or "").strip().lower()
    if pattern in {
        "4_on_4_off_12h",
        "four_on_four_off",
        "four_on_four_off_12_hour",
    }:
        pattern = "four_on_four_off_12h"
    return pattern if pattern in {"none", "four_on_four_off_12h"} else "none"


def _set_staff_shift_pattern_combo(combo: QComboBox, value) -> None:
    pattern = _normalise_staff_shift_pattern_value(value)
    index = combo.findData(pattern)
    if index < 0:
        index = 0
    combo.setCurrentIndex(index)


def _selected_staff_shift_pattern_combo(combo: QComboBox) -> str:
    return _normalise_staff_shift_pattern_value(combo.currentData())


def _make_staff_movement_policy_widget(owner, initial_policy) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    owner.staff_movement_policy_checks = {}

    def select_policy(policy):
        policy = _normalise_staff_movement_policy_value(policy)
        for value, check in owner.staff_movement_policy_checks.items():
            check.blockSignals(True)
            check.setChecked(value == policy)
            check.blockSignals(False)

    for label, value in STAFF_MOVEMENT_POLICIES:
        check = QCheckBox(label)
        owner.staff_movement_policy_checks[value] = check
        layout.addWidget(check)
        check.toggled.connect(
            lambda checked, policy=value: select_policy(policy) if checked else None
        )
    layout.addStretch(1)

    owner._set_staff_movement_policy = select_policy
    owner._selected_staff_movement_policy = lambda: next(
        (
            value
            for value, check in owner.staff_movement_policy_checks.items()
            if check.isChecked()
        ),
        "batch_same_location",
    )
    select_policy(initial_policy)
    return widget



DAY_OPTIONS = [
    ("Mon", "mon"),
    ("Tue", "tue"),
    ("Wed", "wed"),
    ("Thu", "thu"),
    ("Fri", "fri"),
    ("Sat", "sat"),
    ("Sun", "sun"),
]


class DayOfWeekSelector(QWidget):
    """Compact day selector used by scheduling dialogs."""

    def __init__(self, parent=None, selected=None):
        super().__init__(parent)
        selected_values = {
            str(value).strip().lower()[:3]
            for value in (selected or [])
            if str(value).strip()
        }
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        checks_row = QHBoxLayout()
        layout.addLayout(checks_row)
        self.checks = {}
        for label, value in DAY_OPTIONS:
            check = QCheckBox(label)
            check.setChecked(value in selected_values)
            self.checks[value] = check
            checks_row.addWidget(check)
        checks_row.addStretch(1)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        for text, values in [
            ("Weekdays", {"mon", "tue", "wed", "thu", "fri"}),
            ("Every day", {value for _label, value in DAY_OPTIONS}),
            ("Clear", set()),
        ]:
            button = QPushButton(text)
            button.setFlat(True)
            button.clicked.connect(lambda _checked=False, chosen=values: self.set_days(chosen))
            actions.addWidget(button)
        actions.addStretch(1)

    def selected_days(self):
        return [value for _label, value in DAY_OPTIONS if self.checks[value].isChecked()]

    def set_days(self, values):
        chosen = {str(value).strip().lower()[:3] for value in values or []}
        for value, check in self.checks.items():
            check.setChecked(value in chosen)


class PeopleProfilePreview(QWidget):
    """Small preview of the configured people-flow shape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.values = {
            "shape": "constant",
            "minimum": 0,
            "peak": 1,
            "ramp_up": 60.0,
            "ramp_down": 60.0,
            "start": "08:00",
            "end": "18:00",
        }

    @staticmethod
    def _minutes(text):
        try:
            hour, minute = [int(part) for part in str(text).split(":", 1)]
            return hour * 60 + minute
        except Exception:
            return 0

    @staticmethod
    def _count_at(shape, minimum, peak, elapsed, total, ramp_up, ramp_down):
        total = max(0.1, float(total))
        remaining = max(0.0, total - float(elapsed))
        if shape == "ramp_up":
            factor = 1.0 if ramp_up <= 0 else min(1.0, elapsed / ramp_up)
        elif shape == "ramp_down":
            factor = 1.0 if ramp_down <= 0 else min(1.0, remaining / ramp_down)
        elif shape == "ramp_up_down":
            up = 1.0 if ramp_up <= 0 else min(1.0, elapsed / ramp_up)
            down = 1.0 if ramp_down <= 0 else min(1.0, remaining / ramp_down)
            factor = min(up, down)
        else:
            factor = 1.0
        return minimum + (peak - minimum) * max(0.0, min(1.0, factor))

    def set_profile(self, **values):
        self.values.update(values)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(12, 10, -12, -12)
        painter.fillRect(rect, self.palette().base())
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawRect(rect)

        left = rect.left() + 45
        right = rect.right() - 12
        top = rect.top() + 18
        bottom = rect.bottom() - 30
        painter.drawLine(left, bottom, right, bottom)
        painter.drawLine(left, top, left, bottom)

        start = self._minutes(self.values.get("start", "08:00"))
        end = self._minutes(self.values.get("end", "18:00"))
        if end <= start:
            end += 24 * 60
        total = max(1.0, end - start)
        minimum = max(0, int(self.values.get("minimum", 0) or 0))
        peak = max(1, int(self.values.get("peak", 1) or 1))
        shape = str(self.values.get("shape", "constant") or "constant")
        ramp_up = max(0.0, float(self.values.get("ramp_up", 0.0) or 0.0))
        ramp_down = max(0.0, float(self.values.get("ramp_down", 0.0) or 0.0))

        path = QPainterPath()
        samples = 64
        for index in range(samples + 1):
            elapsed = total * index / samples
            count = self._count_at(
                shape, minimum, peak, elapsed, total, ramp_up, ramp_down
            )
            x = left + (right - left) * index / samples
            y = bottom - (bottom - top) * (count / max(1.0, peak))
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(self.palette().highlight().color(), 2))
        painter.drawPath(path)

        painter.setPen(self.palette().text().color())
        painter.drawText(rect.left() + 4, top + 5, str(peak))
        painter.drawText(rect.left() + 8, bottom + 4, "0")
        painter.drawText(left, rect.bottom() - 8, str(self.values.get("start", "")))
        end_text = str(self.values.get("end", ""))
        end_width = painter.fontMetrics().horizontalAdvance(end_text)
        painter.drawText(right - end_width, rect.bottom() - 8, end_text)
        painter.drawText(left, rect.top() + 12, "People per interval")


def _dialog_intro(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("dialogIntro")
    label.setFrameShape(QFrame.StyledPanel)
    label.setContentsMargins(10, 8, 10, 8)
    return label


def _configure_data_table(table: QTableWidget, *, extended=False) -> None:
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(
        QAbstractItemView.ExtendedSelection if extended else QAbstractItemView.SingleSelection
    )
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)


def _double_input(
    value=0.0,
    *,
    minimum=0.0,
    maximum=1_000_000_000.0,
    decimals=3,
    suffix="",
    step=0.1,
    tooltip="",
):
    """Create a consistently configured numeric input with visible units."""
    widget = QDoubleSpinBox()
    widget.setRange(float(minimum), float(maximum))
    widget.setDecimals(int(decimals))
    widget.setSingleStep(float(step))
    widget.setValue(max(float(minimum), min(float(maximum), float(value or 0.0))))
    if suffix:
        widget.setSuffix(str(suffix))
    if tooltip:
        widget.setToolTip(str(tooltip))
    widget.setAccelerated(True)
    return widget


def _integer_input(
    value=0,
    *,
    minimum=0,
    maximum=1_000_000_000,
    suffix="",
    tooltip="",
):
    widget = QSpinBox()
    widget.setRange(int(minimum), int(maximum))
    widget.setValue(max(int(minimum), min(int(maximum), int(float(value or 0)))))
    if suffix:
        widget.setSuffix(str(suffix))
    if tooltip:
        widget.setToolTip(str(tooltip))
    widget.setAccelerated(True)
    return widget


def _set_group_enabled(check: QCheckBox, widgets) -> None:
    enabled = bool(check.isChecked())
    for widget in widgets:
        widget.setEnabled(enabled)


def _selected_rows(table: QTableWidget):
    selection = table.selectionModel()
    return sorted({index.row() for index in selection.selectedRows()}) if selection else []


class ScheduledTimesDialog(QDialog):
    def __init__(self, parent, times=None):
        super().__init__(parent)
        self.setWindowTitle("Scheduled times")
        self.resize(520, 520)

        self.result = None
        self.times = sorted(set(times or []))

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Scheduled task times over a 24 hour day"))

        add_row = QHBoxLayout()
        layout.addLayout(add_row)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime(8, 0))

        add_btn = QPushButton("Add time")
        add_btn.clicked.connect(self.add_time)

        add_row.addWidget(self.time_edit)
        add_row.addWidget(add_btn)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        layout.addLayout(btn_row)

        remove_btn = QPushButton("Remove selected")
        clear_btn = QPushButton("Clear all")
        sort_btn = QPushButton("Sort")

        remove_btn.clicked.connect(self.remove_selected)
        clear_btn.clicked.connect(self.clear_all)
        sort_btn.clicked.connect(self.refresh)

        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(sort_btn)
        btn_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()
        _polish_dialog(self)

    def add_time(self):
        value = self.time_edit.time().toString("HH:mm")
        if value not in self.times:
            self.times.append(value)
            self.times.sort()
        self.refresh()

    def remove_selected(self):
        rows = sorted(
            {index.row() for index in self.list_widget.selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            if 0 <= row < len(self.times):
                del self.times[row]
        self.refresh()

    def clear_all(self):
        self.times = []
        self.refresh()

    def refresh(self):
        self.times = sorted(set(self.times))
        self.list_widget.clear()

        for hour in range(24):
            hour_times = [t for t in self.times if t.startswith(f"{hour:02d}:")]
            if not hour_times:
                continue

            header = QListWidgetItem(f"{hour:02d}:00")
            header.setFlags(Qt.ItemIsEnabled)
            self.list_widget.addItem(header)

            for value in hour_times:
                item = QListWidgetItem(f"    {value}")
                item.setData(Qt.UserRole, value)
                self.list_widget.addItem(item)

    def accept(self):
        self.result = sorted(set(self.times))
        super().accept()



def _default_staff_weekly_hours() -> dict:
    return {
        key: {
            "enabled": key in {"mon", "tue", "wed", "thu", "fri"},
            "start_time": "09:00",
            "end_time": "17:00",
        }
        for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }


def _normalise_staff_weekly_hours(value) -> dict:
    source = value if isinstance(value, dict) else {}
    result = _default_staff_weekly_hours()
    for day_key, fallback in result.items():
        raw = source.get(day_key, {})
        raw = raw if isinstance(raw, dict) else {}
        start_time = str(raw.get("start_time", fallback["start_time"]) or "").strip()
        end_time = str(raw.get("end_time", fallback["end_time"]) or "").strip()
        if not QTime.fromString(start_time, "HH:mm").isValid():
            start_time = fallback["start_time"]
        if not QTime.fromString(end_time, "HH:mm").isValid():
            end_time = fallback["end_time"]
        result[day_key] = {
            "enabled": bool(raw.get("enabled", fallback["enabled"])),
            "start_time": start_time,
            "end_time": end_time,
        }
    return result


def _staff_weekly_hours_summary(use_custom: bool, value) -> str:
    if not use_custom:
        return "Use selected global shift pattern"
    hours = _normalise_staff_weekly_hours(value)
    active = []
    labels = {
        "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
        "fri": "Fri", "sat": "Sat", "sun": "Sun",
    }
    for key in labels:
        item = hours[key]
        if item.get("enabled", False):
            active.append(
                f"{labels[key]} {item.get('start_time', '09:00')}-{item.get('end_time', '17:00')}"
            )
    if not active:
        return "Custom hours: no working days"
    if len(active) <= 3:
        return "; ".join(active)
    return f"{len(active)} custom working days"


class StaffWeeklyHoursDialog(QDialog):
    DAYS = [
        ("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"),
        ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday"),
        ("sun", "Sunday"),
    ]

    def __init__(self, parent, use_custom=False, hours=None):
        super().__init__(parent)
        self.setWindowTitle("Staff working hours by day")
        self.resize(560, 420)
        self.result = None
        self.hours = _normalise_staff_weekly_hours(hours)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Override the global shift hours for this category or department. "
            "These hours control timeframe task spacing and the actual times at "
            "which staff can travel to and handle delivered payloads."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.custom_check = QCheckBox("Use these custom working hours")
        self.custom_check.setChecked(bool(use_custom))
        layout.addWidget(self.custom_check)

        grid = QGridLayout()
        grid.addWidget(QLabel("Working"), 0, 0)
        grid.addWidget(QLabel("Day"), 0, 1)
        grid.addWidget(QLabel("Start"), 0, 2)
        grid.addWidget(QLabel("Finish"), 0, 3)
        self.rows = {}
        for row_index, (day_key, day_label) in enumerate(self.DAYS, start=1):
            item = self.hours[day_key]
            enabled = QCheckBox()
            enabled.setChecked(bool(item.get("enabled", False)))
            start_edit = QTimeEdit()
            start_edit.setDisplayFormat("HH:mm")
            start_edit.setTime(QTime.fromString(item.get("start_time", "09:00"), "HH:mm"))
            end_edit = QTimeEdit()
            end_edit.setDisplayFormat("HH:mm")
            end_edit.setTime(QTime.fromString(item.get("end_time", "17:00"), "HH:mm"))
            enabled.toggled.connect(start_edit.setEnabled)
            enabled.toggled.connect(end_edit.setEnabled)
            start_edit.setEnabled(enabled.isChecked())
            end_edit.setEnabled(enabled.isChecked())
            self.rows[day_key] = (enabled, start_edit, end_edit)
            grid.addWidget(enabled, row_index, 0)
            grid.addWidget(QLabel(day_label), row_index, 1)
            grid.addWidget(start_edit, row_index, 2)
            grid.addWidget(end_edit, row_index, 3)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def accept(self):
        hours = {}
        for day_key, _day_label in self.DAYS:
            enabled, start_edit, end_edit = self.rows[day_key]
            start_time = start_edit.time().toString("HH:mm")
            end_time = end_edit.time().toString("HH:mm")
            if enabled.isChecked() and start_time == end_time:
                QMessageBox.critical(
                    self,
                    "Invalid staff working hours",
                    f"{_day_label} start and finish times cannot be the same.",
                )
                return
            hours[day_key] = {
                "enabled": enabled.isChecked(),
                "start_time": start_time,
                "end_time": end_time,
            }
        if self.custom_check.isChecked() and not any(
            item.get("enabled", False) for item in hours.values()
        ):
            QMessageBox.critical(
                self,
                "Invalid staff working hours",
                "Select at least one working day or disable the custom-hours override.",
            )
            return
        self.result = {
            "use_custom": self.custom_check.isChecked(),
            "hours": hours,
        }
        super().accept()


class GlobalStaffConfigDialog(QDialog):
    """Editor for staff hours shared by all staff-assisted task generators."""

    DAYS = [
        ("mon", "Mon"),
        ("tue", "Tue"),
        ("wed", "Wed"),
        ("thu", "Thu"),
        ("fri", "Fri"),
        ("sat", "Sat"),
        ("sun", "Sun"),
    ]

    def __init__(self, parent, config=None):
        super().__init__(parent)
        self.setWindowTitle("Global staff configuration")
        self.resize(620, 520)
        self.result = None
        self.config = self._normalise(config)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "These working patterns are shared by every task-generation category "
            "that requires staff. Timeframe tasks can be spread across the "
            "selected pattern's working window instead of all being released at "
            "the beginning of the day."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.enabled_check = QCheckBox("Enable global staff working patterns")
        self.enabled_check.setChecked(bool(self.config.get("enabled", True)))
        layout.addWidget(self.enabled_check)

        self.spread_check = QCheckBox(
            "Evenly space staff-assisted timeframe tasks across working hours"
        )
        self.spread_check.setChecked(
            bool(self.config.get("spread_timeframe_tasks", True))
        )
        layout.addWidget(self.spread_check)

        travel_box = QGroupBox("Travel and handling assumptions")
        travel_form = QFormLayout(travel_box)
        self.walking_speed_edit = _double_input(
            self.config.get("walking_speed_m_per_sec", 1.2),
            minimum=0.1,
            maximum=10.0,
            decimals=2,
            suffix=" m/s",
            step=0.1,
            tooltip="Average staff walking speed used when moving between task locations.",
        )
        self.lift_wait_edit = _double_input(
            self.config.get("lift_wait_seconds", 30.0),
            minimum=0.0,
            maximum=3600.0,
            decimals=1,
            suffix=" s",
            step=5.0,
            tooltip="Additional allowance applied when staff change floors using a lift.",
        )
        self.default_handling_edit = _double_input(
            self.config.get("default_handling_minutes", 15.0),
            minimum=0.0,
            maximum=1440.0,
            decimals=1,
            suffix=" min",
            step=1.0,
            tooltip="Default time reserved for staff to receive, exchange or handle a delivered payload.",
        )
        travel_form.addRow("Walking speed", self.walking_speed_edit)
        travel_form.addRow("Lift wait allowance", self.lift_wait_edit)
        travel_form.addRow("Default payload handling", self.default_handling_edit)
        layout.addWidget(travel_box)

        patterns = self.config.get("shift_patterns", {}) or {}
        fixed = patterns.get("none", {}) or {}
        rotating = patterns.get("four_on_four_off_12h", {}) or {}

        layout.addWidget(QLabel("Fixed working-hours pattern"))
        fixed_form = QFormLayout()
        layout.addLayout(fixed_form)
        self.fixed_start_edit = self._time_edit(fixed.get("start_time", "09:00"))
        self.fixed_end_edit = self._time_edit(fixed.get("end_time", "17:00"))
        fixed_form.addRow("Start", self.fixed_start_edit)
        fixed_form.addRow("Finish", self.fixed_end_edit)
        self.fixed_day_checks, fixed_days_widget = self._day_widget(
            fixed.get("days_active", ["mon", "tue", "wed", "thu", "fri"])
        )
        fixed_form.addRow("Working days", fixed_days_widget)

        layout.addSpacing(8)
        layout.addWidget(QLabel("4 on / 4 off, 12-hour pattern"))
        rotating_form = QFormLayout()
        layout.addLayout(rotating_form)
        self.rotating_start_edit = self._time_edit(
            rotating.get("start_time", "07:00")
        )
        self.rotating_end_edit = self._time_edit(
            rotating.get("end_time", "19:00")
        )
        rotating_form.addRow("Shift start", self.rotating_start_edit)
        rotating_form.addRow("Shift finish", self.rotating_end_edit)
        cycle_label = QLabel(
            "Two alternating teams are rostered. Team A and Team B swap every "
            "four days from the simulation start date."
        )
        cycle_label.setWordWrap(True)
        rotating_form.addRow("Cycle", cycle_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    @staticmethod
    def _normalise(config):
        source = config if isinstance(config, dict) else {}
        patterns = source.get("shift_patterns", {})
        if not isinstance(patterns, dict):
            patterns = {}
        fixed = dict(patterns.get("none", {}) or {})
        rotating = dict(patterns.get("four_on_four_off_12h", {}) or {})
        fixed.setdefault("display_name", "Fixed working hours")
        fixed.setdefault("start_time", "09:00")
        fixed.setdefault("end_time", "17:00")
        fixed.setdefault("days_active", ["mon", "tue", "wed", "thu", "fri"])
        fixed["work_days"] = 0
        fixed["rest_days"] = 0
        rotating.setdefault("display_name", "4 on / 4 off, 12-hour days")
        rotating.setdefault("start_time", "07:00")
        rotating.setdefault("end_time", "19:00")
        rotating.setdefault(
            "days_active", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        )
        rotating["work_days"] = 4
        rotating["rest_days"] = 4
        def positive_float(name, default, minimum=0.0):
            try:
                return max(minimum, float(source.get(name, default)))
            except Exception:
                return float(default)

        return {
            "enabled": bool(source.get("enabled", True)),
            "spread_timeframe_tasks": bool(
                source.get(
                    "spread_timeframe_tasks",
                    source.get("space_timeframe_tasks", True),
                )
            ),
            "walking_speed_m_per_sec": positive_float(
                "walking_speed_m_per_sec", 1.2, 0.1
            ),
            "lift_wait_seconds": positive_float("lift_wait_seconds", 30.0, 0.0),
            "default_handling_minutes": positive_float(
                "default_handling_minutes", 15.0, 0.0
            ),
            "shift_patterns": {
                "none": fixed,
                "four_on_four_off_12h": rotating,
            },
        }

    @staticmethod
    def _time_edit(value):
        edit = QTimeEdit()
        edit.setDisplayFormat("HH:mm")
        parsed = QTime.fromString(str(value or ""), "HH:mm")
        if not parsed.isValid():
            parsed = QTime(9, 0)
        edit.setTime(parsed)
        return edit

    def _day_widget(self, active_days):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        active = {str(x).strip().lower()[:3] for x in (active_days or [])}
        checks = {}
        for key, label in self.DAYS:
            check = QCheckBox(label)
            check.setChecked(key in active)
            checks[key] = check
            row.addWidget(check)
        row.addStretch(1)
        return checks, widget

    def accept(self):
        fixed_days = [
            key for key, _label in self.DAYS if self.fixed_day_checks[key].isChecked()
        ]
        if self.enabled_check.isChecked() and not fixed_days:
            QMessageBox.critical(
                self, "Invalid staff configuration", "Select at least one fixed-hours working day."
            )
            return

        fixed_start = self.fixed_start_edit.time().toString("HH:mm")
        fixed_end = self.fixed_end_edit.time().toString("HH:mm")
        rotating_start = self.rotating_start_edit.time().toString("HH:mm")
        rotating_end = self.rotating_end_edit.time().toString("HH:mm")
        if fixed_start == fixed_end or rotating_start == rotating_end:
            QMessageBox.critical(
                self,
                "Invalid staff configuration",
                "A staff shift start and finish time cannot be the same.",
            )
            return

        self.result = {
            "enabled": self.enabled_check.isChecked(),
            "spread_timeframe_tasks": self.spread_check.isChecked(),
            "walking_speed_m_per_sec": float(self.walking_speed_edit.value()),
            "lift_wait_seconds": float(self.lift_wait_edit.value()),
            "default_handling_minutes": float(self.default_handling_edit.value()),
            "shift_patterns": {
                "none": {
                    "display_name": "Fixed working hours",
                    "start_time": fixed_start,
                    "end_time": fixed_end,
                    "days_active": fixed_days,
                    "work_days": 0,
                    "rest_days": 0,
                },
                "four_on_four_off_12h": {
                    "display_name": "4 on / 4 off, 12-hour days",
                    "start_time": rotating_start,
                    "end_time": rotating_end,
                    "days_active": [
                        "mon", "tue", "wed", "thu", "fri", "sat", "sun"
                    ],
                    "work_days": 4,
                    "rest_days": 4,
                },
            },
        }
        super().accept()


class BulkDepartmentTaskGenerationDialog(QDialog):
    DAYS = (
        TaskGenerationSettingsDialog.DAYS
        if "TaskGenerationSettingsDialog" in globals()
        else [
            ("mon", "Mon"),
            ("tue", "Tue"),
            ("wed", "Wed"),
            ("thu", "Thu"),
            ("fri", "Fri"),
            ("sat", "Sat"),
            ("sun", "Sun"),
        ]
    )

    MODES = [
        "scheduled",
        "threshold",
        "continuous",
        "sporadic",
        "hybrid",
        "scheduled_threshold",
        "scheduled_sporadic",
        "timeframe",
    ]

    def __init__(
        self,
        parent,
        category_key,
        category_label,
        departments,
        base_category,
        location_names,
        payload_names,
        profile_names,
        selected_department_ids=None,
        result_key="",
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Configure multiple departments - {category_label}")
        self.resize(920, 720)

        self.category_key = category_key
        self.is_waste_category = str(category_key).strip().lower() == "waste"
        self.departments = [dict(x) for x in departments or []]
        self.base_category = dict(base_category or {})
        self.location_names = sorted(location_names)
        self.payload_names = list(payload_names)
        self.profile_names = list(profile_names)
        self.result = None
        self.result_key = str(result_key or "").strip()
        self.selected_department_ids = list(selected_department_ids or [])
        self.department_location_role = "dropoff"
        self.department_location_role = str(
            self.base_category.get(
                "department_location_role", self.department_location_role
            )
        )
        self.selected_pickup_locations = []
        self.selected_dropoffs = list(self.base_category.get("dropoff_locations", []))
        self.scheduled_times = list(self.base_category.get("scheduled_times", []))
        self.timeframe_start_edit = None
        self.timeframe_end_edit = None
        self.timeframe_payload_multiple_edit = None

        # In the bulk/multiple department configuration dialog, only departments
        # with an existing assigned location for the selected category are valid.
        # This prevents applying generation settings to departments where the
        # category has not yet been placed/assigned in the Department editor.
        self.selected_department_ids = [
            dept_id
            for dept_id in self.selected_department_ids
            if self._department_has_assigned_category_location_by_id(dept_id)
        ]

        layout = QVBoxLayout(self)

        dept_row = QHBoxLayout()
        self.department_summary = QLabel("No departments selected")
        self.department_summary.setWordWrap(True)
        pick_depts_btn = QPushButton("Select departments...")
        pick_depts_btn.clicked.connect(self.pick_departments)
        dept_row.addWidget(self.department_summary, 1)
        dept_row.addWidget(pick_depts_btn)
        layout.addLayout(dept_row)

        role_row = QHBoxLayout()
        layout.addLayout(role_row)

        self.role_pickup_radio = QCheckBox("Use department locations as pickup/source")
        self.role_dropoff_radio = QCheckBox("Use department locations as drop-off")
        self.role_pickup_radio.setChecked(self.department_location_role == "pickup")
        self.role_dropoff_radio.setChecked(self.department_location_role != "pickup")

        self.role_pickup_radio.toggled.connect(
            lambda checked: self.set_department_location_role("pickup", checked)
        )
        self.role_dropoff_radio.toggled.connect(
            lambda checked: self.set_department_location_role("dropoff", checked)
        )

        role_row.addWidget(self.role_pickup_radio)
        role_row.addWidget(self.role_dropoff_radio)
        role_row.addStretch(1)

        form = QFormLayout()
        self.form = form
        layout.addLayout(form)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(self.base_category.get("enabled", False)))

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(self.MODES)
        self.mode_combo.setCurrentText(
            str(self.base_category.get("generation_mode", "scheduled"))
        )

        self.priority_edit = QLineEdit(str(self.base_category.get("priority", 100)))

        legacy_pickup = str(self.base_category.get("pickup_location", "")).strip()
        self.selected_pickup_locations = [legacy_pickup] if legacy_pickup else []

        pickup_row = QHBoxLayout()
        self.pickup_summary = QLabel("None selected")
        self.pickup_summary.setWordWrap(True)

        self.pick_pickups_btn = QPushButton("Select...")
        self.pick_pickups_btn.clicked.connect(self.pick_pickups)

        self.clear_pickups_btn = QPushButton("Clear")
        self.clear_pickups_btn.clicked.connect(self.clear_pickups)

        pickup_row.addWidget(self.pickup_summary, 1)
        pickup_row.addWidget(self.pick_pickups_btn)
        pickup_row.addWidget(self.clear_pickups_btn)

        dropoff_row = QHBoxLayout()
        self.dropoff_summary = QLabel()
        self.dropoff_summary.setWordWrap(True)

        self.pick_dropoffs_btn = QPushButton("Select...")
        self.pick_dropoffs_btn.clicked.connect(self.pick_dropoffs)

        self.clear_dropoffs_btn = QPushButton("Clear")
        self.clear_dropoffs_btn.clicked.connect(self.clear_dropoffs)

        dropoff_row.addWidget(self.dropoff_summary, 1)
        dropoff_row.addWidget(self.pick_dropoffs_btn)
        dropoff_row.addWidget(self.clear_dropoffs_btn)

        self.payload_combo = QComboBox()
        self.payload_combo.addItems([""] + self.payload_names)

        self.payload_combo.setCurrentText(str(self.base_category.get("payload", "")))
        self.delivery_resource_combo = _make_delivery_resource_combo(
            self.base_category.get(
                "delivery_resource", self.base_category.get("delivery_method", "amr")
            )
        )

        self.tracked_item_exchange_check = QCheckBox(
            "Generate tracked item exchange tasks"
        )
        self.tracked_item_exchange_check.setChecked(
            bool(self.base_category.get("tracked_item_exchange", False))
        )

        self.exchange_mode_combo = QComboBox()
        self.exchange_mode_combo.addItems(
            [
                "full_exchange",
                "top_up_only",
                "replace_empty",
            ]
        )
        self.exchange_mode_combo.setCurrentText(
            str(self.base_category.get("exchange_mode", "top_up_only"))
        )

        self.route_profile_combo = QComboBox()
        self.route_profile_combo.addItems([""] + self.profile_names)
        self.route_profile_combo.setCurrentText(
            str(self.base_category.get("route_profile", ""))
        )

        self.return_enabled_check = QCheckBox("Generate return / exchange task")
        self.return_enabled_check.setChecked(
            bool(self.base_category.get("return_enabled", False))
        )

        self.return_payload_combo = QComboBox()
        self.return_payload_combo.addItems([""] + self.payload_names)
        self.return_payload_combo.setCurrentText(
            str(self.base_category.get("return_payload", ""))
        )

        self.return_delay_edit = QLineEdit(
            str(self.base_category.get("return_delay_minutes", 0))
        )
        self.staff_handling_minutes_edit = QLineEdit(
            str(self.base_category.get("staff_handling_minutes", 15.0))
        )
        self.requires_staff_check = QCheckBox("Assign category staff for delivered payload handling")
        self.requires_staff_check.setToolTip(
            "Use a separate staff pool for this category. Staff are reserved for delivered payload handling at the drop-off location."
        )
        self.requires_staff_check.setChecked(
            bool(self.base_category.get("requires_staff", self.base_category.get("staff_required", False)))
        )
        self.staff_initial_count_edit = QLineEdit(
            str(self.base_category.get("staff_initial_count", 1))
        )
        self.staff_resource_name_edit = QLineEdit(
            str(self.base_category.get("staff_resource_name", ""))
        )
        self.staff_resource_name_edit.setPlaceholderText("Optional, e.g. Stores team")
        policy = str(
            self.base_category.get("staff_movement_policy", "batch_same_location")
            or ""
        ).strip()
        self.staff_movement_policy_widget = _make_staff_movement_policy_widget(
            self, policy
        )
        self.staff_shift_pattern_combo = QComboBox()
        for label, value in STAFF_SHIFT_PATTERNS:
            self.staff_shift_pattern_combo.addItem(label, value)
        self.staff_shift_pattern_combo.setToolTip(
            "Select how staff are grouped into shift teams for delivered payload handling."
        )
        _set_staff_shift_pattern_combo(
            self.staff_shift_pattern_combo,
            self.base_category.get("staff_shift_pattern", "none"),
        )
        self.staff_use_custom_working_hours = bool(
            self.base_category.get("staff_use_custom_working_hours", False)
        )
        self.staff_working_hours = _normalise_staff_weekly_hours(
            self.base_category.get("staff_working_hours", {})
        )
        self.staff_hours_widget = QWidget()
        staff_hours_row = QHBoxLayout(self.staff_hours_widget)
        staff_hours_row.setContentsMargins(0, 0, 0, 0)
        self.staff_hours_summary = QLabel()
        self.staff_hours_summary.setWordWrap(True)
        edit_staff_hours_btn = QPushButton("Edit...")
        edit_staff_hours_btn.clicked.connect(self.edit_staff_working_hours)
        staff_hours_row.addWidget(self.staff_hours_summary, 1)
        staff_hours_row.addWidget(edit_staff_hours_btn)

        self.reusable_return_pool_check = QCheckBox(
            "Reuse returned payloads as a capped source pool"
        )
        self.reusable_return_pool_check.setToolTip(
            "Use for simple non-item-tracked flows such as catering trolleys. "
            "Returned payloads replenish the pickup/source pool instead of "
            "accumulating as unlimited new physical stock."
        )
        self.reusable_return_pool_check.setChecked(
            bool(self.base_category.get("reusable_return_pool_enabled", False))
        )
        self.reusable_return_pool_multiplier_edit = QLineEdit(
            str(self.base_category.get("reusable_return_pool_multiplier", 2.0))
        )
        self.reusable_return_pool_max_edit = QLineEdit(
            str(self.base_category.get("reusable_return_pool_max", 0))
        )
        self.reusable_return_pool_max_edit.setToolTip(
            "Optional hard cap for the source pool. Use 0 for automatic: "
            "selected departments × multiplier."
        )

        days_widget = QWidget()
        days_layout = QHBoxLayout(days_widget)
        days_layout.setContentsMargins(0, 0, 0, 0)
        self.day_checks = {}
        active_days = set(
            self.base_category.get("days_active", ["mon", "tue", "wed", "thu", "fri"])
        )
        for key, label in self.DAYS:
            chk = QCheckBox(label)
            chk.setChecked(key in active_days)
            self.day_checks[key] = chk
            days_layout.addWidget(chk)
        days_layout.addStretch(1)

        self.run_every_fortnight_check = QCheckBox("Run every fortnight")
        self.run_every_fortnight_check.setToolTip(
            "When enabled, generated tasks run in week 1, skip week 2, then repeat every other week from the simulation start date."
        )
        self.run_every_fortnight_check.setChecked(
            bool(self.base_category.get("run_every_fortnight", False))
        )

        self.schedule_widget = QWidget()
        schedule_row = QHBoxLayout(self.schedule_widget)
        schedule_row.setContentsMargins(0, 0, 0, 0)
        self.schedule_summary = QLabel()
        self.schedule_summary.setWordWrap(True)
        edit_times_btn = QPushButton("Edit times...")
        edit_times_btn.clicked.connect(self.edit_scheduled_times)
        clear_times_btn = QPushButton("Clear")
        clear_times_btn.clicked.connect(self.clear_scheduled_times)
        self.schedule_button = edit_times_btn
        self.clear_schedule_button = clear_times_btn
        schedule_row.addWidget(self.schedule_summary, 1)
        schedule_row.addWidget(edit_times_btn)
        schedule_row.addWidget(clear_times_btn)

        self.frequency_edit = QLineEdit(
            str(self.base_category.get("frequency_per_day", 0.0))
        )
        self.volume_per_event_edit = QLineEdit(
            str(self.base_category.get("volume_per_event_m3", 0.0))
        )
        self.threshold_volume_edit = QLineEdit(
            str(self.base_category.get("threshold_volume_m3", 0.0))
        )
        self.base_daily_volume_edit = QLineEdit(
            str(self.base_category.get("base_daily_volume_m3", 0.0))
        )
        self.timeframe_start_edit = QLineEdit(
            str(self.base_category.get("timeframe_start", "09:00"))
        )
        self.timeframe_end_edit = QLineEdit(
            str(self.base_category.get("timeframe_end", "17:00"))
        )
        self.timeframe_payload_multiple_edit = QLineEdit(
            str(self.base_category.get("timeframe_payload_multiple", self.base_category.get("payload_multiple", 1)))
        )
        self.notes_edit = QPlainTextEdit(str(self.base_category.get("notes", "")))
        self.notes_edit.setFixedHeight(90)

        positive_double_validator = QDoubleValidator(0.0, 999999.0, 3, self)
        positive_int_validator = QIntValidator(1, 999999, self)
        self.priority_edit.setValidator(QIntValidator(0, 999999, self))
        self.return_delay_edit.setValidator(QDoubleValidator(0.0, 999999.0, 2, self))
        self.staff_handling_minutes_edit.setValidator(
            QDoubleValidator(0.0, 1440.0, 2, self)
        )
        self.staff_initial_count_edit.setValidator(QIntValidator(1, 999999, self))
        self.reusable_return_pool_multiplier_edit.setValidator(QDoubleValidator(0.0, 999999.0, 3, self))
        self.reusable_return_pool_max_edit.setValidator(QIntValidator(0, 999999, self))
        self.frequency_edit.setValidator(positive_double_validator)
        self.volume_per_event_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.threshold_volume_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.base_daily_volume_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.timeframe_payload_multiple_edit.setValidator(positive_int_validator)
        self.timeframe_start_edit.setInputMask("99:99")
        self.timeframe_end_edit.setInputMask("99:99")

        form.addRow("Enabled", self.enabled_check)
        form.addRow("Generation mode", self.mode_combo)
        form.addRow("Priority", self.priority_edit)
        form.addRow("Pickup / source locations", pickup_row)
        form.addRow("Drop-off destinations", dropoff_row)
        form.addRow("Payload", self.payload_combo)
        form.addRow("Delivery resource", self.delivery_resource_combo)
        form.addRow("Tracked item exchange", self.tracked_item_exchange_check)
        form.addRow("Exchange mode", self.exchange_mode_combo)
        form.addRow("Route profile", self.route_profile_combo)
        form.addRow("Return task", self.return_enabled_check)
        form.addRow("Return payload", self.return_payload_combo)
        form.addRow("Return delay (minutes)", self.return_delay_edit)
        form.addRow("Staff handling", self.requires_staff_check)
        form.addRow("Handling time (minutes)", self.staff_handling_minutes_edit)
        form.addRow("Initial staff count", self.staff_initial_count_edit)
        form.addRow("Staff resource name", self.staff_resource_name_edit)
        form.addRow("Staff movement", self.staff_movement_policy_widget)
        form.addRow("Shift pattern", self.staff_shift_pattern_combo)
        form.addRow("Working hours by day", self.staff_hours_widget)
        form.addRow("Reusable return pool", self.reusable_return_pool_check)
        form.addRow("Pool multiplier", self.reusable_return_pool_multiplier_edit)
        form.addRow("Pool hard cap (0 = auto)", self.reusable_return_pool_max_edit)
        form.addRow("Days active", days_widget)
        form.addRow("Fortnightly recurrence", self.run_every_fortnight_check)
        form.addRow("Scheduled times", self.schedule_widget)
        form.addRow("Frequency per day", self.frequency_edit)
        form.addRow("Volume per event m³", self.volume_per_event_edit)
        form.addRow("Threshold volume m³", self.threshold_volume_edit)
        form.addRow("Base daily volume m³", self.base_daily_volume_edit)
        form.addRow("Timeframe start HH:MM", self.timeframe_start_edit)
        form.addRow("Timeframe end HH:MM", self.timeframe_end_edit)
        form.addRow("Payload multiple", self.timeframe_payload_multiple_edit)
        form.addRow("Notes", self.notes_edit)

        self.waste_stream_notice_label = QLabel(
            "For Waste, stream-specific generation settings are applied from "
            "Departments → Manage waste streams. This dialog only applies collection "
            "routing, destination, priority and return-task settings to the selected departments."
        )
        self.waste_stream_notice_label.setWordWrap(True)
        layout.addWidget(self.waste_stream_notice_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_combo.currentTextChanged.connect(self.update_role_field_state)
        self.return_enabled_check.toggled.connect(self.update_role_field_state)
        self.requires_staff_check.toggled.connect(self.update_role_field_state)
        self.reusable_return_pool_check.toggled.connect(self.update_role_field_state)
        self.tracked_item_exchange_check.toggled.connect(self.update_role_field_state)
        self.refresh_department_summary()
        self.refresh_pickup_summary()
        self.update_role_field_state()
        self.refresh_dropoff_summary()
        self.refresh_schedule_summary()
        self.refresh_staff_working_hours_summary()
        _polish_dialog(self)

    def edit_staff_working_hours(self):
        dialog = StaffWeeklyHoursDialog(
            self,
            self.staff_use_custom_working_hours,
            self.staff_working_hours,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.staff_use_custom_working_hours = bool(
                dialog.result.get("use_custom", False)
            )
            self.staff_working_hours = _normalise_staff_weekly_hours(
                dialog.result.get("hours", {})
            )
            self.refresh_staff_working_hours_summary()

    def refresh_staff_working_hours_summary(self):
        self.staff_hours_summary.setText(
            _staff_weekly_hours_summary(
                self.staff_use_custom_working_hours, self.staff_working_hours
            )
        )

    def set_department_location_role(self, role, checked):
        if not checked:
            return

        self.department_location_role = role

        self.role_pickup_radio.blockSignals(True)
        self.role_dropoff_radio.blockSignals(True)

        self.role_pickup_radio.setChecked(role == "pickup")
        self.role_dropoff_radio.setChecked(role == "dropoff")

        self.role_pickup_radio.blockSignals(False)
        self.role_dropoff_radio.blockSignals(False)

        self.update_role_field_state()

    def _set_form_row_visible(self, field_widget, visible):
        visible = bool(visible)
        if field_widget is None:
            return
        try:
            field_widget.setVisible(visible)
        except Exception:
            pass
        try:
            label = self.form.labelForField(field_widget)
            if label is not None:
                label.setVisible(visible)
        except Exception:
            pass

    def update_role_field_state(self):
        using_dept_as_pickup = self.department_location_role == "pickup"

        self.pickup_summary.setEnabled(not using_dept_as_pickup)
        self.pick_pickups_btn.setEnabled(not using_dept_as_pickup)
        self.clear_pickups_btn.setEnabled(not using_dept_as_pickup)

        self.dropoff_summary.setEnabled(using_dept_as_pickup)
        self.pick_dropoffs_btn.setEnabled(using_dept_as_pickup)
        self.clear_dropoffs_btn.setEnabled(using_dept_as_pickup)

        mode = self.mode_combo.currentText().strip()
        is_waste = getattr(self, "is_waste_category", False)
        uses_schedule = mode in {"scheduled", "scheduled_threshold", "scheduled_sporadic"}
        uses_threshold = mode in {"threshold", "hybrid", "scheduled_threshold"}
        uses_continuous = mode in {"continuous", "hybrid"}
        uses_sporadic = mode in {"sporadic", "hybrid", "scheduled_sporadic"}
        uses_timeframe = mode == "timeframe"
        return_enabled = self.return_enabled_check.isChecked()
        show_generation = not is_waste
        staff_enabled = show_generation and self.requires_staff_check.isChecked()

        if is_waste:
            self.mode_combo.setEnabled(False)
            self.payload_combo.setEnabled(False)
            self.tracked_item_exchange_check.setEnabled(False)
            self.exchange_mode_combo.setEnabled(False)
            self._set_form_row_visible(self.exchange_mode_combo, False)
            self.waste_stream_notice_label.setVisible(True)
        elif hasattr(self, "waste_stream_notice_label"):
            self.mode_combo.setEnabled(True)
            self.payload_combo.setEnabled(True)
            self.tracked_item_exchange_check.setEnabled(True)
            self.exchange_mode_combo.setEnabled(self.tracked_item_exchange_check.isChecked())
            self._set_form_row_visible(self.exchange_mode_combo, self.tracked_item_exchange_check.isChecked())
            self.waste_stream_notice_label.setVisible(False)

        self._set_form_row_visible(self.schedule_widget, show_generation and uses_schedule)
        self._set_form_row_visible(self.run_every_fortnight_check, show_generation)
        self._set_form_row_visible(self.frequency_edit, show_generation and uses_sporadic)
        self._set_form_row_visible(self.volume_per_event_edit, show_generation and uses_sporadic)
        self._set_form_row_visible(self.threshold_volume_edit, show_generation and uses_threshold)
        self._set_form_row_visible(self.base_daily_volume_edit, show_generation and (uses_continuous or uses_threshold))
        self._set_form_row_visible(self.timeframe_start_edit, show_generation and uses_timeframe)
        self._set_form_row_visible(self.timeframe_end_edit, show_generation and uses_timeframe)
        self._set_form_row_visible(self.timeframe_payload_multiple_edit, show_generation and uses_timeframe)

        self._set_form_row_visible(self.return_payload_combo, return_enabled)
        self._set_form_row_visible(self.return_delay_edit, return_enabled)
        self._set_form_row_visible(self.requires_staff_check, show_generation)
        self._set_form_row_visible(self.staff_handling_minutes_edit, staff_enabled)
        self._set_form_row_visible(self.staff_initial_count_edit, staff_enabled)
        self._set_form_row_visible(self.staff_resource_name_edit, staff_enabled)
        self._set_form_row_visible(self.staff_movement_policy_widget, staff_enabled)
        self._set_form_row_visible(self.staff_shift_pattern_combo, staff_enabled)
        self._set_form_row_visible(self.staff_hours_widget, staff_enabled)
        self.requires_staff_check.setEnabled(show_generation)
        self.staff_handling_minutes_edit.setEnabled(staff_enabled)
        self.staff_initial_count_edit.setEnabled(staff_enabled)
        self.staff_resource_name_edit.setEnabled(staff_enabled)
        self.staff_movement_policy_widget.setEnabled(staff_enabled)
        self.staff_shift_pattern_combo.setEnabled(staff_enabled)
        self.staff_hours_widget.setEnabled(staff_enabled)
        self._set_form_row_visible(self.reusable_return_pool_check, return_enabled)
        self._set_form_row_visible(self.reusable_return_pool_multiplier_edit, return_enabled and self.reusable_return_pool_check.isChecked())
        self._set_form_row_visible(self.reusable_return_pool_max_edit, return_enabled and self.reusable_return_pool_check.isChecked())

        self.schedule_summary.setEnabled(show_generation and uses_schedule)
        self.schedule_button.setEnabled(show_generation and uses_schedule)
        self.clear_schedule_button.setEnabled(show_generation and uses_schedule)

    def _department_location_picker_rows(self):
        """Return picker rows for departments with assigned category locations.

        The picker should be readable to the user, so it shows the department
        identity/name.  The saved task-generation value must still be the real
        placed location assigned to this category.
        """
        placed_locations = {
            str(x).strip() for x in self.location_names if str(x).strip()
        }

        rows = []
        used_labels = set()

        sorted_departments = sorted(
            self.departments,
            key=lambda d: (
                (
                    int(d.get("floor", 0))
                    if str(d.get("floor", "")).strip().lstrip("-").isdigit()
                    else 999999
                ),
                str(d.get("name", "")).strip().lower()
                or str(d.get("id", "")).strip().lower(),
            ),
        )

        for dept in sorted_departments:
            dept_id = self._department_id_for_item(dept)
            if not dept_id:
                continue

            dept_name = str(dept.get("name", "")).strip()
            floor = str(dept.get("floor", "Other")).strip() or "Other"
            base_label = f"{dept_name} ({dept_id})" if dept_name else dept_id

            assigned_locations = []
            for location_name in self._category_locations_for_department(dept):
                if location_name in placed_locations:
                    assigned_locations.append(location_name)

            for location_name in sorted(set(assigned_locations)):
                label = f"{base_label}    [{location_name}]"
                if label in used_labels:
                    label = f"{base_label}    [{location_name}]    Floor {floor}"
                used_labels.add(label)
                rows.append(
                    {
                        "label": label,
                        "location": location_name,
                        "floor_group": f"Floor {floor}",
                        "department_id": dept_id,
                        "department_name": dept_name,
                    }
                )

        return rows

    def _department_label_for_location(self, location_name):
        location_name = str(location_name or "").strip()
        if not location_name:
            return ""

        for row in self._department_location_picker_rows():
            if row.get("location") == location_name:
                return row.get("label", location_name)

        return location_name

    def pick_pickups(self):
        rows = self._department_location_picker_rows()

        if not rows:
            QMessageBox.information(
                self,
                "Select pickup / source locations",
                (
                    f"No departments have an assigned placed location for "
                    f"the {self.category_key} category.\n\n"
                    "Assign or auto-assign category locations in the Department "
                    "editor first."
                ),
            )
            return

        label_to_location = {row["label"]: row["location"] for row in rows}
        label_to_group = {row["label"]: row["floor_group"] for row in rows}

        selected_labels = [
            row["label"]
            for row in rows
            if row["location"] in set(self.selected_pickup_locations)
        ]

        picker = MultiSelectPicker(
            self,
            "Select pickup / source department locations",
            [row["label"] for row in rows],
            selected=selected_labels,
            group_resolver=lambda item: label_to_group.get(item, "Departments"),
        )

        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_pickup_locations = sorted(
                {
                    label_to_location[label]
                    for label in picker.result
                    if label in label_to_location
                }
            )
            self.refresh_pickup_summary()

    def clear_pickups(self):
        self.selected_pickup_locations = []
        self.refresh_pickup_summary()

    def refresh_pickup_summary(self):
        if not self.selected_pickup_locations:
            self.pickup_summary.setText("None selected")
            return

        display_values = [
            self._department_label_for_location(location_name)
            for location_name in self.selected_pickup_locations
        ]

        if len(display_values) <= 4:
            self.pickup_summary.setText(", ".join(display_values))
        else:
            self.pickup_summary.setText(f"{len(display_values)} selected")

    def _department_id_for_item(self, dept):
        return str(dept.get("id", "")).strip() or str(dept.get("name", "")).strip()

    def _category_locations_for_department(self, dept):
        category_locations = dept.get("task_generation_locations", {}) or {}

        if not isinstance(category_locations, dict):
            return []

        category_entry = category_locations.get(self.category_key, {})

        if isinstance(category_entry, dict):
            raw_locations = category_entry.get(
                "pickup_dropoff_locations",
                category_entry.get("locations", []),
            )
        else:
            raw_locations = category_entry

        return [str(x).strip() for x in (raw_locations or []) if str(x).strip()]

    def _department_has_assigned_category_location(self, dept):
        placed_locations = {
            str(x).strip() for x in self.location_names if str(x).strip()
        }

        return any(
            location_name in placed_locations
            for location_name in self._category_locations_for_department(dept)
        )

    def _department_has_assigned_category_location_by_id(self, dept_id):
        dept_id = str(dept_id or "").strip()
        if not dept_id:
            return False

        for dept in self.departments:
            if self._department_id_for_item(dept) == dept_id:
                return self._department_has_assigned_category_location(dept)

        return False

    def pick_departments(self):
        options = []
        label_by_id = {}
        floor_by_label = {}

        sorted_departments = sorted(
            self.departments,
            key=lambda d: (
                int(d.get("floor", 0)),
                str(d.get("name", "")).strip().lower()
                or str(d.get("id", "")).strip().lower(),
            ),
        )

        for dept in sorted_departments:
            dept_id = self._department_id_for_item(dept)
            if not dept_id:
                continue

            # Only show departments that have a valid placed/assigned location
            # for the category currently being configured.
            if not self._department_has_assigned_category_location(dept):
                continue

            name = str(dept.get("name", "")).strip()
            floor = str(dept.get("floor", "Other")).strip() or "Other"
            category_locations = self._category_locations_for_department(dept)
            location_summary = ", ".join(category_locations[:3])
            if len(category_locations) > 3:
                location_summary += f", +{len(category_locations) - 3}"

            display = f"{dept_id} - {name}" if name else dept_id
            if location_summary:
                display = f"{display}    [{location_summary}]"

            options.append(display)
            label_by_id[display] = dept_id
            floor_by_label[display] = f"Floor {floor}"

        if not options:
            QMessageBox.information(
                self,
                "Select departments",
                (
                    f"No departments have an assigned placed location for "
                    f"the {self.category_key} category.\n\n"
                    "Assign or auto-assign category locations in the Department "
                    "editor first."
                ),
            )
            return

        picker = MultiSelectPicker(
            self,
            "Select departments",
            options,
            selected=[
                display
                for display, dept_id in label_by_id.items()
                if dept_id in self.selected_department_ids
            ],
            group_resolver=lambda item: floor_by_label.get(item, "Other"),
        )

        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_department_ids = [
                label_by_id[x] for x in picker.result if x in label_by_id
            ]

            self.refresh_department_summary()

    def _set_department_location_mode(self, dept_id, mode, checked, other_toggle):
        if not checked:
            return

        self.department_location_modes[dept_id] = mode

        other_toggle.blockSignals(True)
        other_toggle.setChecked(False)
        other_toggle.blockSignals(False)

    def refresh_department_summary(self):
        if not self.selected_department_ids:
            self.department_summary.setText("No departments selected")
        elif len(self.selected_department_ids) <= 6:
            self.department_summary.setText(", ".join(self.selected_department_ids))
        else:
            self.department_summary.setText(
                f"{len(self.selected_department_ids)} departments selected"
            )

    def pick_dropoffs(self):
        picker = MultiSelectPicker(
            self,
            "Select drop-off destinations",
            self.location_names,
            selected=self.selected_dropoffs,
            group_resolver=lambda item: "Locations",
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_dropoffs = sorted(picker.result)
            self.refresh_dropoff_summary()

    def clear_dropoffs(self):
        self.selected_dropoffs = []
        self.refresh_dropoff_summary()

    def refresh_dropoff_summary(self):
        if not self.selected_dropoffs:
            self.dropoff_summary.setText("None selected")
        elif len(self.selected_dropoffs) <= 4:
            self.dropoff_summary.setText(", ".join(self.selected_dropoffs))
        else:
            self.dropoff_summary.setText(f"{len(self.selected_dropoffs)} selected")

    def edit_scheduled_times(self):
        dialog = ScheduledTimesDialog(self, self.scheduled_times)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.scheduled_times = list(dialog.result)
            self.refresh_schedule_summary()

    def clear_scheduled_times(self):
        self.scheduled_times = []
        self.refresh_schedule_summary()

    def refresh_schedule_summary(self):
        if not self.scheduled_times:
            self.schedule_summary.setText("No times selected")
        elif len(self.scheduled_times) <= 8:
            self.schedule_summary.setText(", ".join(self.scheduled_times))
        else:
            self.schedule_summary.setText(f"{len(self.scheduled_times)} times selected")

    def _department_default_location(self, dept_id):
        placed_locations = {
            str(x).strip() for x in self.location_names if str(x).strip()
        }

        for dept in self.departments:
            current_id = self._department_id_for_item(dept)
            if current_id != dept_id:
                continue

            for location_name in self._category_locations_for_department(dept):
                if location_name in placed_locations:
                    return location_name

        return ""

    def accept(self):
        try:
            if not self.selected_department_ids:
                raise ValueError("Select at least one department")

            days_active = [
                key for key, _label in self.DAYS if self.day_checks[key].isChecked()
            ]
            if not days_active:
                raise ValueError("Select at least one active day")

            dropoff_locations = [
                str(x).strip() for x in self.selected_dropoffs if str(x).strip()
            ]

            payload = {
                "enabled": self.enabled_check.isChecked(),
                "generation_mode": self.mode_combo.currentText().strip(),
                "priority": int(float(self.priority_edit.text() or 100)),
                "pickup_location": (
                    self.selected_pickup_locations[0]
                    if self.selected_pickup_locations
                    else ""
                ),
                "pickup_locations": list(self.selected_pickup_locations),
                "dropoff_location": dropoff_locations[0] if dropoff_locations else "",
                "dropoff_locations": dropoff_locations,
                "payload": self.payload_combo.currentText().strip(),
                "delivery_resource": normalise_delivery_resource_policy(
                    {"mode": self.delivery_resource_combo.currentData()}
                ),
                "tracked_item_exchange": self.tracked_item_exchange_check.isChecked(),
                "exchange_mode": self.exchange_mode_combo.currentText().strip(),
                "return_enabled": self.return_enabled_check.isChecked(),
                "return_payload": self.return_payload_combo.currentText().strip(),
                "return_delay_minutes": float(self.return_delay_edit.text() or 0),
                "requires_staff": self.requires_staff_check.isChecked(),
                "staff_handling_minutes": max(
                    0.0, float(self.staff_handling_minutes_edit.text() or 0.0)
                ),
                "staff_initial_count": max(1, int(float(self.staff_initial_count_edit.text() or 1))),
                "staff_resource_name": self.staff_resource_name_edit.text().strip(),
                "staff_movement_policy": self._selected_staff_movement_policy(),
                "staff_shift_pattern": _selected_staff_shift_pattern_combo(
                    self.staff_shift_pattern_combo
                ),
                "staff_use_custom_working_hours": bool(
                    self.staff_use_custom_working_hours
                ),
                "staff_working_hours": _normalise_staff_weekly_hours(
                    self.staff_working_hours
                ),
                "reusable_return_pool_enabled": self.reusable_return_pool_check.isChecked(),
                "reusable_return_pool_multiplier": float(self.reusable_return_pool_multiplier_edit.text() or 2.0),
                "reusable_return_pool_max": int(float(self.reusable_return_pool_max_edit.text() or 0)),
                "route_profile": self.route_profile_combo.currentText().strip(),
                "days_active": days_active,
                "run_every_fortnight": self.run_every_fortnight_check.isChecked(),
                "scheduled_times": list(self.scheduled_times),
                "frequency_per_day": float(self.frequency_edit.text() or 0.0),
                "volume_per_event_m3": float(self.volume_per_event_edit.text() or 0.0),
                "threshold_volume_m3": float(self.threshold_volume_edit.text() or 0.0),
                "base_daily_volume_m3": float(
                    self.base_daily_volume_edit.text() or 0.0
                ),
                "timeframe_start": self.timeframe_start_edit.text().strip(),
                "timeframe_end": self.timeframe_end_edit.text().strip(),
                "timeframe_payload_multiple": int(float(self.timeframe_payload_multiple_edit.text() or 1)),
                "payload_multiple": int(float(self.timeframe_payload_multiple_edit.text() or 1)),
                "notes": self.notes_edit.toPlainText().strip(),
            }

            if self.is_waste_category:
                payload["generation_mode"] = "threshold"
                payload["payload"] = ""
                payload["tracked_item_exchange"] = False
                payload["exchange_mode"] = "top_up_only"
                payload["scheduled_times"] = []
                payload["run_every_fortnight"] = False
                payload["frequency_per_day"] = 0.0
                payload["volume_per_event_m3"] = 0.0
                payload["threshold_volume_m3"] = 0.0
                payload["base_daily_volume_m3"] = 0.0
                payload["timeframe_start"] = ""
                payload["timeframe_end"] = ""
                payload["timeframe_payload_multiple"] = 1
                payload["payload_multiple"] = 1

            self.result = {}

            for dept_id in self.selected_department_ids:
                item = dict(payload)
                role = self.department_location_role
                dept_location = self._department_default_location(dept_id)

                if role == "pickup":
                    item["pickup_location"] = dept_location
                    item["pickup_locations"] = [dept_location] if dept_location else []
                    item["dropoff_location"] = (
                        dropoff_locations[0] if dropoff_locations else ""
                    )
                    item["dropoff_locations"] = list(dropoff_locations)
                else:
                    item["pickup_location"] = (
                        self.selected_pickup_locations[0]
                        if self.selected_pickup_locations
                        else ""
                    )
                    item["pickup_locations"] = list(self.selected_pickup_locations)
                    item["dropoff_location"] = dept_location
                    item["dropoff_locations"] = [dept_location] if dept_location else []

                item["department_location_role"] = role
                self.result[dept_id] = item

            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid bulk department settings", str(exc))


class ConfiguredGroupSelectDialog(QDialog):
    def __init__(self, parent, title, groups, label_builder):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 520)
        self.result_key = None

        layout = QVBoxLayout(self)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list_widget, 1)

        for signature, group in groups.items():
            item = QListWidgetItem(label_builder(group))
            item.setData(Qt.UserRole, signature)
            item.setSizeHint(item.sizeHint())
            self.list_widget.addItem(item)

        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def accept(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        self.result_key = item.data(Qt.UserRole)
        super().accept()


class TaskGenerationSettingsDialog(QDialog):
    """Editor for top-level task_generation logistics parameters."""

    DAYS = [
        ("mon", "Mon"),
        ("tue", "Tue"),
        ("wed", "Wed"),
        ("thu", "Thu"),
        ("fri", "Fri"),
        ("sat", "Sat"),
        ("sun", "Sun"),
    ]

    CATEGORY_LABELS = [
        ("catering", "Catering"),
        ("pharmacy", "Pharmacy"),
        ("linen", "Linen"),
        ("waste", "Waste"),
        ("stores", "Stores"),
        ("ssd", "SSD"),
    ]

    MODES = [
        "scheduled",
        "threshold",
        "continuous",
        "sporadic",
        "hybrid",
        "scheduled_threshold",
        "scheduled_sporadic",
        "timeframe",
    ]

    def __init__(
        self,
        parent,
        task_generation,
        location_names,
        payload_names,
        profile_names,
        departments,
        on_save,
    ):
        super().__init__(parent)
        self.setWindowTitle("Task generation parameters")
        self.resize(1060, 720)
        self.location_names = sorted(location_names)
        self.payload_names = sorted(payload_names)
        self.profile_names = list(profile_names)
        self.departments = [dict(x) for x in (departments or [])]
        self.current_department_id = None
        self.on_save = on_save
        self.current_key = None
        self.selected_dropoffs = []
        self._loading = False
        self.config = self._normalise_config(task_generation)
        self.staff_config = json.loads(
            json.dumps(self.config.get("staff_config", {}))
        )

        layout = QVBoxLayout(self)

        self.global_enabled = QCheckBox("Enable automatic task generation")
        self.global_enabled.setChecked(bool(self.config.get("enabled", True)))
        layout.addWidget(self.global_enabled)

        global_staff_row = QHBoxLayout()
        self.global_staff_summary = QLabel()
        self.global_staff_summary.setWordWrap(True)
        edit_global_staff_btn = QPushButton("Global staff configuration...")
        edit_global_staff_btn.clicked.connect(self.edit_global_staff_config)
        global_staff_row.addWidget(self.global_staff_summary, 1)
        global_staff_row.addWidget(edit_global_staff_btn)
        layout.addLayout(global_staff_row)
        self._refresh_global_staff_summary()

        body = QHBoxLayout()
        layout.addLayout(body, 1)

        left = QVBoxLayout()
        body.addLayout(left, 0)

        lists_row = QHBoxLayout()
        left.addLayout(lists_row, 1)

        category_col = QVBoxLayout()
        lists_row.addLayout(category_col)

        category_col.addWidget(QLabel("Categories"))
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(190)
        category_col.addWidget(self.category_list, 1)

        department_col = QVBoxLayout()
        lists_row.addLayout(department_col)

        department_col.addWidget(QLabel("Departments"))
        self.department_list = QListWidget()
        self.department_list.setFixedWidth(230)
        department_col.addWidget(self.department_list, 1)

        self.department_hint = QLabel(
            "Select a department to configure department-specific task generation"
        )
        self.department_hint.setWordWrap(True)
        department_col.addWidget(self.department_hint)

        bulk_dept_btn = QPushButton("Configure multiple...")
        bulk_dept_btn.clicked.connect(self.configure_multiple_departments)
        department_col.addWidget(bulk_dept_btn)

        edit_group_btn = QPushButton("Edit configured group...")
        edit_group_btn.clicked.connect(self.edit_configured_department_group)
        department_col.addWidget(edit_group_btn)

        delete_group_btn = QPushButton("Delete configured group...")
        delete_group_btn.clicked.connect(self.delete_configured_department_group)
        department_col.addWidget(delete_group_btn)

        clear_group_btn = QPushButton("Clear configured group...")
        clear_group_btn.clicked.connect(self.clear_configured_department_group)
        department_col.addWidget(clear_group_btn)

        clear_category_departments_btn = QPushButton("Clear all department settings...")
        clear_category_departments_btn.setToolTip(
            "Remove every department-specific task generation override for the selected category."
        )
        clear_category_departments_btn.clicked.connect(
            self.clear_all_department_settings_for_category
        )
        department_col.addWidget(clear_category_departments_btn)

        category_buttons = QHBoxLayout()
        left.addLayout(category_buttons)
        add_category_btn = QPushButton("Add")
        delete_category_btn = QPushButton("Delete")
        add_category_btn.clicked.connect(self.add_category)
        delete_category_btn.clicked.connect(self.delete_current_category)
        category_buttons.addWidget(add_category_btn)
        category_buttons.addWidget(delete_category_btn)

        right = QScrollArea()
        right.setWidgetResizable(True)
        body.addWidget(right, 1)
        container = QWidget()
        right.setWidget(container)
        form = QFormLayout(container)
        self.form = form
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.enabled_check = QCheckBox("Enabled")
        self.display_name_edit = QLineEdit()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(self.MODES)
        self.priority_edit = QLineEdit()

        self.role_pickup_radio = QCheckBox("Use department locations as pickup/source")
        self.role_dropoff_radio = QCheckBox("Use department locations as drop-off")
        self.role_dropoff_radio.setChecked(True)
        self.role_pickup_radio.toggled.connect(
            lambda checked: self._set_department_location_role("pickup", checked)
        )
        self.role_dropoff_radio.toggled.connect(
            lambda checked: self._set_department_location_role("dropoff", checked)
        )
        role_row = QHBoxLayout()
        role_row.addWidget(self.role_pickup_radio)
        role_row.addWidget(self.role_dropoff_radio)
        role_row.addStretch(1)

        self.pickup_combo = QComboBox()
        self.pickup_combo.addItems([""] + self.location_names)

        self.dropoff_summary = QLabel("None selected")
        self.dropoff_summary.setWordWrap(True)
        dropoff_row = QHBoxLayout()
        dropoff_row.addWidget(self.dropoff_summary, 1)
        self.pick_dropoffs_btn = QPushButton("Select...")
        self.pick_dropoffs_btn.clicked.connect(self.pick_dropoff_locations)

        self.clear_dropoffs_btn = QPushButton("Clear")
        self.clear_dropoffs_btn.clicked.connect(self.clear_dropoff_locations)

        dropoff_row.addWidget(self.dropoff_summary, 1)
        dropoff_row.addWidget(self.pick_dropoffs_btn)
        dropoff_row.addWidget(self.clear_dropoffs_btn)

        self.payload_combo = QComboBox()
        self.payload_combo.addItems([""] + self.payload_names)
        self.delivery_resource_combo = _make_delivery_resource_combo("amr")

        self.tracked_item_exchange_check = QCheckBox(
            "Generate tracked item exchange tasks"
        )

        self.exchange_mode_combo = QComboBox()
        self.exchange_mode_combo.addItems(
            [
                "full_exchange",
                "top_up_only",
                "replace_empty",
            ]
        )

        self.route_profile_combo = QComboBox()
        self.route_profile_combo.addItems([""] + self.profile_names)

        self.return_enabled_check = QCheckBox("Generate return / exchange task")
        self.return_payload_combo = QComboBox()
        self.return_payload_combo.addItems([""] + self.payload_names)

        self.return_delay_edit = QLineEdit()
        self.staff_handling_minutes_edit = QLineEdit()
        self.requires_staff_check = QCheckBox("Assign category staff for delivered payload handling")
        self.requires_staff_check.setToolTip(
            "Use a separate staff pool for this category. Staff are reserved for delivered payload handling at the drop-off location."
        )
        self.staff_initial_count_edit = QLineEdit()
        self.staff_resource_name_edit = QLineEdit()
        self.staff_resource_name_edit.setPlaceholderText("Optional, e.g. Stores team")
        self.staff_movement_policy_widget = _make_staff_movement_policy_widget(
            self, "batch_same_location"
        )
        self.staff_shift_pattern_combo = QComboBox()
        for label, value in STAFF_SHIFT_PATTERNS:
            self.staff_shift_pattern_combo.addItem(label, value)
        self.staff_shift_pattern_combo.setToolTip(
            "Select how staff are grouped into shift teams for delivered payload handling."
        )
        self.staff_use_custom_working_hours = False
        self.staff_working_hours = _normalise_staff_weekly_hours({})
        self.staff_hours_widget = QWidget()
        staff_hours_row = QHBoxLayout(self.staff_hours_widget)
        staff_hours_row.setContentsMargins(0, 0, 0, 0)
        self.staff_hours_summary = QLabel()
        self.staff_hours_summary.setWordWrap(True)
        edit_staff_hours_btn = QPushButton("Edit...")
        edit_staff_hours_btn.clicked.connect(self.edit_staff_working_hours)
        staff_hours_row.addWidget(self.staff_hours_summary, 1)
        staff_hours_row.addWidget(edit_staff_hours_btn)

        self.reusable_return_pool_check = QCheckBox(
            "Reuse returned payloads as a capped source pool"
        )
        self.reusable_return_pool_check.setToolTip(
            "Use for simple non-item-tracked flows such as catering trolleys. "
            "Returned payloads replenish the pickup/source pool instead of "
            "accumulating as unlimited new physical stock."
        )
        self.reusable_return_pool_multiplier_edit = QLineEdit()
        self.reusable_return_pool_max_edit = QLineEdit()
        self.reusable_return_pool_max_edit.setToolTip(
            "Optional hard cap for the source pool. Use 0 for automatic: "
            "selected departments × multiplier."
        )

        days_widget = QWidget()
        days_layout = QHBoxLayout(days_widget)
        days_layout.setContentsMargins(0, 0, 0, 0)
        self.day_checks = {}
        for key, label in self.DAYS:
            chk = QCheckBox(label)
            self.day_checks[key] = chk
            days_layout.addWidget(chk)
        days_layout.addStretch(1)

        self.run_every_fortnight_check = QCheckBox("Run every fortnight")
        self.run_every_fortnight_check.setToolTip(
            "When enabled, generated tasks run in week 1, skip week 2, then repeat every other week from the simulation start date."
        )

        self.scheduled_times = []

        self.schedule_widget = QWidget()
        schedule_row = QHBoxLayout(self.schedule_widget)
        schedule_row.setContentsMargins(0, 0, 0, 0)
        self.schedule_summary = QLabel("No times selected")
        self.schedule_summary.setWordWrap(True)

        schedule_btn = QPushButton("Edit times...")
        schedule_btn.clicked.connect(self.edit_scheduled_times)

        clear_schedule_btn = QPushButton("Clear")
        clear_schedule_btn.clicked.connect(self.clear_scheduled_times)

        schedule_row.addWidget(self.schedule_summary, 1)
        schedule_row.addWidget(schedule_btn)
        schedule_row.addWidget(clear_schedule_btn)

        self.schedule_button = schedule_btn
        self.clear_schedule_button = clear_schedule_btn

        self.frequency_edit = QLineEdit()
        self.volume_per_event_edit = QLineEdit()
        self.threshold_volume_edit = QLineEdit()
        self.base_daily_volume_edit = QLineEdit()
        self.timeframe_start_edit = QLineEdit()
        self.timeframe_start_edit.setPlaceholderText("HH:MM, e.g. 09:00")
        self.timeframe_end_edit = QLineEdit()
        self.timeframe_end_edit.setPlaceholderText("HH:MM, e.g. 17:00")
        self.timeframe_payload_multiple_edit = QLineEdit()
        self.timeframe_payload_multiple_edit.setPlaceholderText("1")
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setFixedHeight(90)

        positive_double_validator = QDoubleValidator(0.0, 999999.0, 3, self)
        positive_int_validator = QIntValidator(1, 999999, self)
        self.priority_edit.setValidator(QIntValidator(0, 999999, self))
        self.return_delay_edit.setValidator(QDoubleValidator(0.0, 999999.0, 2, self))
        self.staff_handling_minutes_edit.setValidator(
            QDoubleValidator(0.0, 1440.0, 2, self)
        )
        self.staff_initial_count_edit.setValidator(QIntValidator(1, 999999, self))
        self.reusable_return_pool_multiplier_edit.setValidator(QDoubleValidator(0.0, 999999.0, 3, self))
        self.reusable_return_pool_max_edit.setValidator(QIntValidator(0, 999999, self))
        self.frequency_edit.setValidator(positive_double_validator)
        self.volume_per_event_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.threshold_volume_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.base_daily_volume_edit.setValidator(QDoubleValidator(0.0, 999999.0, 6, self))
        self.timeframe_payload_multiple_edit.setValidator(positive_int_validator)
        self.timeframe_start_edit.setInputMask("99:99")
        self.timeframe_end_edit.setInputMask("99:99")

        form.addRow("Category enabled", self.enabled_check)
        form.addRow("Display name", self.display_name_edit)
        form.addRow("Generation mode", self.mode_combo)
        form.addRow("Priority", self.priority_edit)
        form.addRow("Department location role", role_row)
        form.addRow("Pickup / source location", self.pickup_combo)
        form.addRow("Drop-off destinations", dropoff_row)
        form.addRow("Payload", self.payload_combo)
        form.addRow("Delivery resource", self.delivery_resource_combo)
        form.addRow("Tracked item exchange", self.tracked_item_exchange_check)
        form.addRow("Exchange mode", self.exchange_mode_combo)
        form.addRow("Route profile", self.route_profile_combo)
        form.addRow("Return task", self.return_enabled_check)
        form.addRow("Return payload", self.return_payload_combo)
        form.addRow("Return delay (minutes)", self.return_delay_edit)
        form.addRow("Staff handling", self.requires_staff_check)
        form.addRow("Handling time (minutes)", self.staff_handling_minutes_edit)
        form.addRow("Initial staff count", self.staff_initial_count_edit)
        form.addRow("Staff resource name", self.staff_resource_name_edit)
        form.addRow("Staff movement", self.staff_movement_policy_widget)
        form.addRow("Shift pattern", self.staff_shift_pattern_combo)
        form.addRow("Working hours by day", self.staff_hours_widget)
        form.addRow("Reusable return pool", self.reusable_return_pool_check)
        form.addRow("Pool multiplier", self.reusable_return_pool_multiplier_edit)
        form.addRow("Pool hard cap (0 = auto)", self.reusable_return_pool_max_edit)
        form.addRow("Days active", days_widget)
        form.addRow("Fortnightly recurrence", self.run_every_fortnight_check)

        form.addRow("Scheduled times", self.schedule_widget)

        form.addRow("Frequency per day", self.frequency_edit)
        form.addRow("Volume per event m³", self.volume_per_event_edit)
        form.addRow("Threshold volume m³", self.threshold_volume_edit)
        form.addRow("Base daily volume m³", self.base_daily_volume_edit)
        form.addRow("Timeframe start HH:MM", self.timeframe_start_edit)
        form.addRow("Timeframe end HH:MM", self.timeframe_end_edit)
        form.addRow("Payload multiple", self.timeframe_payload_multiple_edit)
        form.addRow("Notes", self.notes_edit)

        self.waste_stream_notice_label = QLabel(
            "Waste task generation uses the waste streams assigned in the Departments dialog. "
            "Edit generation mode, frequency, volume per event, threshold and scheduled times via "
            "Departments → Manage waste streams. The Waste category here only controls enablement, "
            "priority, pickup/drop-off role, destinations, route profile and return task settings."
        )
        self.waste_stream_notice_label.setWordWrap(True)
        layout.addWidget(self.waste_stream_notice_label)

        help_label = QLabel(
            "Schedule times are comma-separated HH:MM values. "
            "Timeframe mode releases non-staff tasks at the start of the active-day window. "
            "Staff-assisted timeframe tasks are evenly spaced across the applicable category/department working hours (or the global shift pattern when no override is selected) and remain due before the effective timeframe end. "
            "Drop-off destinations can contain multiple locations; the first is also saved as "
            "dropoff_location for compatibility with existing generators. "
            "Configure multiple can be used more than once for the same category and department; "
            "each configured group creates an additional run in the simulator. "
            "For Waste, stream-specific generated volume is configured on the departments' waste streams, "
            "not on the task-generation category override."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_combo.currentTextChanged.connect(self._update_mode_field_state)
        self.return_enabled_check.toggled.connect(self._update_mode_field_state)
        self.requires_staff_check.toggled.connect(self._update_mode_field_state)
        self.reusable_return_pool_check.toggled.connect(self._update_mode_field_state)
        self.tracked_item_exchange_check.toggled.connect(self._update_mode_field_state)

        self.category_list.currentItemChanged.connect(self._on_category_changed)
        self.department_list.currentItemChanged.connect(self._on_department_changed)

        self._refresh_category_list()

        if self.category_list.count() > 0:
            self.category_list.setCurrentRow(0)
            current = self.category_list.currentItem()
            if current is not None:
                self.current_key = current.data(Qt.UserRole)

        self.current_department_id = ""

        self._loading = True
        self._refresh_department_list(select_dept_id="")
        if self.current_key:
            self._load_category(self.current_key)
        self._loading = False

        self._refresh_schedule_summary()
        self._refresh_staff_working_hours_summary()
        _polish_dialog(self)

    def edit_staff_working_hours(self):
        dialog = StaffWeeklyHoursDialog(
            self,
            self.staff_use_custom_working_hours,
            self.staff_working_hours,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.staff_use_custom_working_hours = bool(
                dialog.result.get("use_custom", False)
            )
            self.staff_working_hours = _normalise_staff_weekly_hours(
                dialog.result.get("hours", {})
            )
            self._refresh_staff_working_hours_summary()

    def _refresh_staff_working_hours_summary(self):
        self.staff_hours_summary.setText(
            _staff_weekly_hours_summary(
                self.staff_use_custom_working_hours, self.staff_working_hours
            )
        )

    def _department_display_name(self, dept_id):
        dept_id = str(dept_id).strip()

        for dept in self.departments:
            current_id = (
                str(dept.get("id", "")).strip() or str(dept.get("name", "")).strip()
            )
            if current_id == dept_id:
                name = str(dept.get("name", "")).strip()
                floor = str(dept.get("floor", "")).strip()
                if floor:
                    return f"{name or dept_id} (Floor {floor})"
                return name or dept_id

        return dept_id

    def _department_group_names_text(self, dept_ids, limit=4):
        names = [self._department_display_name(x) for x in sorted(dept_ids)]

        if len(names) <= limit:
            return ", ".join(names)

        shown = ", ".join(names[:limit])
        remaining = len(names) - limit
        return f"{shown}, +{remaining} departments"

    def _configured_group_label(self, group):
        payload = group.get("payload", {}) or {}
        dept_ids = sorted(group.get("departments", []))

        category_label = (
            self.category_list.currentItem().text()
            if self.category_list.currentItem()
            else self.current_key
        )

        payload_name = str(payload.get("payload", "")).strip() or "No payload"
        role = str(payload.get("department_location_role", "")).strip() or "default"
        mode = str(payload.get("generation_mode", "")).strip() or "default"
        pickup = str(payload.get("pickup_location", "")).strip() or "None"
        dropoff = str(payload.get("dropoff_location", "")).strip() or "None"
        delay = str(payload.get("return_delay_minutes", "")).strip()
        requires_staff = bool(payload.get("requires_staff", payload.get("staff_required", False)))
        staff_count = str(payload.get("staff_initial_count", 1) or 1).strip()
        staff_name = str(payload.get("staff_resource_name", "") or "").strip()

        lines = [
            f"{category_label}    Payload: {payload_name}    Mode: {mode}",
            f"Departments: {self._department_group_names_text(dept_ids, limit=4)}",
            f"Role: {role}    Pickup: {pickup}    Drop-off: {dropoff}",
        ]

        if delay not in {"", "0", "0.0"}:
            lines.append(f"Return delay: {delay} min")
        if requires_staff:
            staff_text = f"Staff: {staff_count} initial"
            if staff_name:
                staff_text += f" ({staff_name})"
            if (
                _normalise_staff_shift_pattern_value(
                    payload.get("staff_shift_pattern", "none")
                )
                == "four_on_four_off_12h"
            ):
                staff_text += " + 4 on / 4 off allowance"
            handling = payload.get("staff_handling_minutes", 15.0)
            staff_text += f"; handling {handling:g} min" if isinstance(handling, (int, float)) else ""
            lines.append(staff_text)
            lines.append(
                "Hours: " + _staff_weekly_hours_summary(
                    bool(payload.get("staff_use_custom_working_hours", False)),
                    payload.get("staff_working_hours", {}),
                )
            )

        return "\n".join(lines)

    def _current_form_has_generation_settings(self):
        if self.enabled_check.isChecked():
            return True

        if self.pickup_combo.currentText().strip():
            return True

        if self.selected_dropoffs:
            return True

        if self.payload_combo.currentText().strip():
            return True

        if self.delivery_resource_combo.currentData() != "amr":
            return True

        if self.return_enabled_check.isChecked():
            return True

        if self.return_payload_combo.currentText().strip():
            return True

        if hasattr(self, "requires_staff_check") and self.requires_staff_check.isChecked():
            return True

        if self.route_profile_combo.currentText().strip():
            return True

        if self.scheduled_times:
            return True

        numeric_fields = [
            self.frequency_edit,
            self.volume_per_event_edit,
            self.threshold_volume_edit,
            self.base_daily_volume_edit,
        ]

        for widget in numeric_fields:
            try:
                if float(widget.text() or 0.0) != 0.0:
                    return True
            except Exception:
                if widget.text().strip():
                    return True

        if self.mode_combo.currentText().strip() == "timeframe":
            if self._normalise_hhmm_text(self.timeframe_start_edit.text()):
                return True

            if self._normalise_hhmm_text(self.timeframe_end_edit.text()):
                return True

            try:
                if self._int_from_edit(self.timeframe_payload_multiple_edit, 1) != 1:
                    return True
            except Exception:
                if self.timeframe_payload_multiple_edit.text().strip():
                    return True

        if self.notes_edit.toPlainText().strip():
            return True

        if hasattr(self, "tracked_item_exchange_check"):
            if self.tracked_item_exchange_check.isChecked():
                return True

        return False

    def _blank_category_clear_settings(self, category_key, existing_category=None):
        """Return a disabled/blank category config used by the clear-all action.

        The clear-all action must not expose or save the built-in category defaults
        back into every department.  Keeping the display name preserves the category
        row in the editor, while all operational values are reset to disabled/blank.
        """
        category_key = str(category_key or "").strip()
        existing_category = dict(existing_category or {})
        display_name = str(
            existing_category.get("display_name", category_key.title())
        ).strip() or category_key.title()
        is_waste = category_key.lower() == "waste"

        return {
            "enabled": False,
            "display_name": display_name,
            "generation_mode": "threshold" if is_waste else "scheduled",
            "uses_department_waste_streams": bool(
                existing_category.get("uses_department_waste_streams", is_waste)
            ),
            "priority": 0,
            "department_location_role": "dropoff",
            "pickup_location": "",
            "pickup_locations": [],
            "dropoff_location": "",
            "dropoff_locations": [],
            "payload": "",
            "delivery_resource": normalise_delivery_resource_policy("amr"),
            "tracked_item_exchange": False,
            "exchange_mode": "top_up_only",
            "return_enabled": False,
            "return_payload": "",
            "return_delay_minutes": 0.0,
            "requires_staff": False,
            "staff_handling_minutes": 15.0,
            "staff_initial_count": 1,
            "staff_resource_name": "",
            "staff_movement_policy": "batch_same_location",
            "staff_shift_pattern": "none",
            "staff_use_custom_working_hours": False,
            "staff_working_hours": {},
            "reusable_return_pool_enabled": False,
            "reusable_return_pool_multiplier": 0.0,
            "reusable_return_pool_max": 0,
            "route_profile": "",
            "days_active": [],
            "run_every_fortnight": False,
            "scheduled_times": [],
            "frequency_per_day": 0.0,
            "volume_per_event_m3": 0.0,
            "threshold_volume_m3": 0.0,
            "base_daily_volume_m3": 0.0,
            "timeframe_start": "",
            "timeframe_end": "",
            "timeframe_payload_multiple": 1,
            "payload_multiple": 1,
            "notes": "",
            "departments": {},
            "department_groups": [],
        }

    def clear_all_department_settings_for_category(self):
        if not self.current_key:
            QMessageBox.information(
                self,
                "Clear department settings",
                "Select a category first.",
            )
            return

        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        category = self.config.setdefault("categories", {}).setdefault(
            self.current_key, {}
        )

        overrides = category.get("departments", {})
        if not isinstance(overrides, dict):
            overrides = {}

        groups = self._department_groups_for_category(self.current_key)
        override_count = len(overrides)
        group_count = len(groups)

        has_category_settings = any(
            [
                bool(category.get("enabled", False)),
                str(category.get("pickup_location", "")).strip(),
                category.get("pickup_locations"),
                str(category.get("dropoff_location", "")).strip(),
                category.get("dropoff_locations"),
                str(category.get("payload", "")).strip(),
                bool(category.get("tracked_item_exchange", False)),
                bool(category.get("return_enabled", False)),
                str(category.get("return_payload", "")).strip(),
                str(category.get("route_profile", "")).strip(),
                category.get("days_active"),
                category.get("scheduled_times"),
                float(category.get("priority", 0) or 0) != 0.0,
                float(category.get("frequency_per_day", 0.0) or 0.0) != 0.0,
                float(category.get("volume_per_event_m3", 0.0) or 0.0) != 0.0,
                float(category.get("threshold_volume_m3", 0.0) or 0.0) != 0.0,
                float(category.get("base_daily_volume_m3", 0.0) or 0.0) != 0.0,
                str(category.get("notes", "")).strip(),
            ]
        )

        if override_count == 0 and group_count == 0 and not has_category_settings:
            QMessageBox.information(
                self,
                "Clear department settings",
                "No task-generation settings were found for this category.",
            )
            return

        category_label = (
            self.category_list.currentItem().text()
            if self.category_list.currentItem()
            else str(self.current_key)
        )

        message = (
            f"Clear all task-generation settings for '{category_label}'?\n\n"
            f"This will remove {override_count} department override(s)"
        )
        if group_count:
            message += f" and {group_count} configured group(s)"
        message += (
            ".\n\nThe selected category will also be reset to blank/disabled values, "
            "so cleared departments do not inherit the category defaults. "
            "Department location assignments are kept."
        )

        if (
            QMessageBox.question(
                self,
                "Clear department settings",
                message,
            )
            != QMessageBox.Yes
        ):
            return

        self.config.setdefault("categories", {})[self.current_key] = (
            self._blank_category_clear_settings(self.current_key, category)
        )

        if str(self.current_key).strip().lower() == "waste":
            self.config["department_waste"] = {"enabled": False, "priority": 0}

        selected_dept_id = self.current_department_id or ""

        self._loading = True
        self._refresh_category_list(select_key=self.current_key)
        self._refresh_department_list(select_dept_id=selected_dept_id)
        if self.current_key:
            self._load_category(self.current_key)
        self._loading = False

        QMessageBox.information(
            self,
            "Clear department settings",
            f"Cleared {override_count} department override(s) and reset the category to blank/disabled values.",
        )

    def clear_configured_department_group(self):
        if not self.current_key:
            QMessageBox.information(
                self,
                "Clear configured group",
                "Select a category first.",
            )
            return

        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        category = self.config.setdefault("categories", {}).setdefault(
            self.current_key, {}
        )
        overrides = category.setdefault("departments", {})
        groups = self._stored_department_group_map(self.current_key)

        if not groups:
            QMessageBox.information(
                self,
                "Clear configured group",
                "No multi-department configured groups were found for this category.",
            )
            return

        dialog = ConfiguredGroupSelectDialog(
            self,
            "Clear configured group",
            groups,
            self._configured_group_label,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result_key:
            return

        group = groups[dialog.result_key]
        dept_ids = sorted(group["departments"])

        if (
            QMessageBox.question(
                self,
                "Clear configured group",
                (
                    f"Clear task-generation overrides for {len(dept_ids)} department(s)?\n\n"
                    + ", ".join(dept_ids[:12])
                    + ("..." if len(dept_ids) > 12 else "")
                ),
            )
            != QMessageBox.Yes
        ):
            return

        for dept_id in dept_ids:
            overrides.pop(dept_id, None)

        category["department_groups"] = [
            group
            for group in self._department_groups_for_category(self.current_key)
            if str(group.get("id", "")).strip() != str(dialog.result_key).strip()
        ]

        remaining_dept = None

        for index in range(self.department_list.count()):
            item = self.department_list.item(index)
            dept_id = str(item.data(Qt.UserRole) or "").strip()
            if dept_id and dept_id not in dept_ids:
                remaining_dept = dept_id
                break

        self._loading = True
        self._refresh_department_list(select_dept_id=remaining_dept)

        if self.department_list.count() > 0:
            current = self.department_list.currentItem()
            self.current_department_id = current.data(Qt.UserRole) if current else ""
            self.current_department_id = self.current_department_id or ""
            self._load_category(self.current_key)
        else:
            self.current_department_id = ""
            self._clear_task_generation_form()

        self._loading = False

        self._loading = True
        self._refresh_department_list(select_dept_id="")
        self._load_category(self.current_key)
        self._loading = False

        QMessageBox.information(
            self,
            "Clear configured group",
            f"Cleared {len(dept_ids)} department override(s).",
        )

    def delete_configured_department_group(self):
        if not self.current_key:
            QMessageBox.information(
                self,
                "Delete configured group",
                "Select a category first.",
            )
            return

        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        category = self.config.setdefault("categories", {}).setdefault(
            self.current_key, {}
        )
        overrides = category.setdefault("departments", {})
        if not isinstance(overrides, dict):
            overrides = {}
            category["departments"] = overrides

        groups = self._stored_department_group_map(self.current_key)

        if not groups:
            QMessageBox.information(
                self,
                "Delete configured group",
                "No multi-department configured groups were found for this category.",
            )
            return

        dialog = ConfiguredGroupSelectDialog(
            self,
            "Delete configured group",
            groups,
            self._configured_group_label,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result_key:
            return

        group_id = str(dialog.result_key).strip()
        group = groups[group_id]
        dept_ids = sorted(group["departments"])
        payload_signature = self._bulk_group_signature(group.get("payload", {}))

        if (
            QMessageBox.question(
                self,
                "Delete configured group",
                (
                    f"Delete this configured run for {len(dept_ids)} department(s)?\n\n"
                    + ", ".join(dept_ids[:12])
                    + ("..." if len(dept_ids) > 12 else "")
                ),
            )
            != QMessageBox.Yes
        ):
            return

        remaining_groups = [
            group
            for group in self._department_groups_for_category(self.current_key)
            if str(group.get("id", "")).strip() != group_id
        ]
        category["department_groups"] = remaining_groups

        remaining_grouped_departments = {
            str(dept_id).strip()
            for group in remaining_groups
            for dept_id in group.get("departments", [])
            if str(dept_id).strip()
        }

        # Bulk configuration also writes per-department overrides for editor
        # compatibility. Remove matching orphaned overrides so deleting the last
        # group does not leave an ungrouped simulator run behind.
        removed_overrides = 0
        for dept_id in dept_ids:
            if dept_id in remaining_grouped_departments:
                continue
            current_payload = overrides.get(dept_id, {})
            if (
                isinstance(current_payload, dict)
                and self._bulk_group_signature(current_payload) == payload_signature
            ):
                overrides.pop(dept_id, None)
                removed_overrides += 1

        selected_dept_id = self.current_department_id or ""
        if selected_dept_id in dept_ids and selected_dept_id not in overrides:
            selected_dept_id = ""

        self._loading = True
        self._refresh_department_list(select_dept_id=selected_dept_id)
        if self.current_key:
            self._load_category(self.current_key)
        self._loading = False

        message = "Deleted the configured group."
        if removed_overrides:
            message += f" Removed {removed_overrides} matching compatibility override(s)."

        QMessageBox.information(
            self,
            "Delete configured group",
            message,
        )

    def _bulk_group_signature(self, payload):
        return json.dumps(payload or {}, sort_keys=True)

    def _department_groups_for_category(self, category_key):
        category = self.config.setdefault("categories", {}).setdefault(category_key, {})
        groups = category.setdefault("department_groups", [])

        if not isinstance(groups, list):
            groups = []
            category["department_groups"] = groups

        clean_groups = []
        seen_ids = set()

        for group in groups:
            if not isinstance(group, dict):
                continue

            group_id = str(group.get("id", "")).strip()
            if not group_id:
                group_id = self._new_department_group_id(category)

            if group_id in seen_ids:
                group_id = self._new_department_group_id(category)

            departments = [
                str(x).strip() for x in group.get("departments", []) if str(x).strip()
            ]

            payload = group.get("payload", {})
            if not departments or not isinstance(payload, dict):
                continue
            payload = dict(payload)
            self._normalise_staff_movement_policy(payload)
            self._normalise_staff_shift_pattern(payload)

            seen_ids.add(group_id)
            clean_groups.append(
                {
                    "id": group_id,
                    "departments": sorted(set(departments)),
                    "payload": payload,
                }
            )

        category["department_groups"] = clean_groups
        return clean_groups

    def _new_department_group_id(self, category):
        existing = {
            str(group.get("id", "")).strip()
            for group in category.get("department_groups", [])
            if isinstance(group, dict)
        }

        counter = 1
        while True:
            group_id = f"GROUP-{counter}"
            if group_id not in existing:
                return group_id
            counter += 1

    def _stored_department_group_map(self, category_key):
        groups = self._department_groups_for_category(category_key)
        return {
            str(group.get("id", "")).strip(): group
            for group in groups
            if str(group.get("id", "")).strip()
        }

    def _remove_departments_from_groups(
        self,
        category_key,
        department_ids,
        except_group_id=None,
    ):
        department_ids = {str(x).strip() for x in department_ids if str(x).strip()}

        if not department_ids:
            return

        groups = self._department_groups_for_category(category_key)
        kept_groups = []

        for group in groups:
            group_id = str(group.get("id", "")).strip()

            if except_group_id and group_id == str(except_group_id).strip():
                kept_groups.append(group)
                continue

            group["departments"] = [
                dept_id
                for dept_id in group.get("departments", [])
                if str(dept_id).strip() not in department_ids
            ]

            if group["departments"]:
                kept_groups.append(group)

        category = self.config.setdefault("categories", {}).setdefault(category_key, {})
        category["department_groups"] = kept_groups

    def _remove_department_from_group_if_settings_changed(
        self,
        category_key,
        department_id,
        payload,
    ):
        department_id = str(department_id or "").strip()
        if not department_id:
            return

        current_signature = self._bulk_group_signature(payload)
        groups = self._department_groups_for_category(category_key)
        changed = False

        for group in groups:
            group_payload = group.get("payload", {})
            group_signature = self._bulk_group_signature(group_payload)

            if department_id not in group.get("departments", []):
                continue

            if group_signature != current_signature:
                group["departments"] = [
                    dept_id
                    for dept_id in group.get("departments", [])
                    if dept_id != department_id
                ]
                changed = True

        if changed:
            category = self.config.setdefault("categories", {}).setdefault(
                category_key, {}
            )
            category["department_groups"] = [
                group for group in groups if group.get("departments")
            ]

    def _upsert_department_group(self, category_key, group_id, department_ids, payload):
        category = self.config.setdefault("categories", {}).setdefault(category_key, {})
        groups = self._department_groups_for_category(category_key)

        group_id = str(group_id or "").strip()
        if not group_id:
            group_id = self._new_department_group_id(category)

        department_ids = sorted(
            {str(x).strip() for x in department_ids if str(x).strip()}
        )

        if not department_ids:
            return ""

        existing = next(
            (group for group in groups if str(group.get("id", "")).strip() == group_id),
            None,
        )

        if existing is None:
            groups.append(
                {
                    "id": group_id,
                    "departments": department_ids,
                    "payload": dict(payload),
                }
            )
        else:
            existing["departments"] = department_ids
            existing["payload"] = dict(payload)

        category["department_groups"] = [
            group for group in groups if group.get("departments")
        ]

        return group_id

    def edit_configured_department_group(self):
        if not self.current_key:
            QMessageBox.information(
                self,
                "Edit configured group",
                "Select a category first.",
            )
            return

        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        category = self.config.setdefault("categories", {}).setdefault(
            self.current_key, {}
        )
        overrides = category.setdefault("departments", {})
        groups = self._stored_department_group_map(self.current_key)

        if not groups:
            QMessageBox.information(
                self,
                "Edit configured group",
                "No multi-department configured groups were found for this category.",
            )
            return

        dialog = ConfiguredGroupSelectDialog(
            self,
            "Edit configured group",
            groups,
            self._configured_group_label,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result_key:
            return

        group = groups[dialog.result_key]
        group_payload = dict(group["payload"])
        selected_department_ids = sorted(group["departments"])

        dialog = BulkDepartmentTaskGenerationDialog(
            self,
            category_key=self.current_key,
            category_label=(
                self.category_list.currentItem().text()
                if self.category_list.currentItem()
                else self.current_key
            ),
            departments=self.departments,
            base_category=group_payload,
            location_names=self.location_names,
            payload_names=self.payload_names,
            profile_names=self.profile_names,
            selected_department_ids=selected_department_ids,
            result_key=dialog.result_key,
        )

        if dialog.exec() == QDialog.Accepted and dialog.result:
            result_dept_ids = sorted(dialog.result.keys())
            result_payload = next(iter(dialog.result.values()))

            for dept_id in selected_department_ids:
                overrides.pop(dept_id, None)

            for dept_id, payload in dialog.result.items():
                overrides[dept_id] = payload

            self._upsert_department_group(
                self.current_key,
                getattr(dialog, "result_key", ""),
                result_dept_ids,
                result_payload,
            )

            self._load_category(self.current_key)

    def _department_label(self, dept):
        name = str(dept.get("name", "")).strip()
        dept_id = str(dept.get("id", "")).strip()
        enabled = bool(dept.get("enabled", True))

        label = name or dept_id or "Department"
        if dept_id and dept_id != label:
            label = f"{label} ({dept_id})"

        if not enabled:
            label += " [disabled]"

        return label

    def _refresh_department_list(self, select_dept_id=None):
        current_dept_id = str(
            select_dept_id or self.current_department_id or ""
        ).strip()

        self.department_list.blockSignals(True)
        self.department_list.clear()

        selected_row = 0
        valid_departments = []

        for dept in self.departments:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                dept_id = str(dept.get("name", "")).strip()

            if not dept_id:
                continue

            valid_departments.append((dept_id, dept))

        valid_departments.sort(
            key=lambda item: (
                (
                    int(item[1].get("floor", 0))
                    if str(item[1].get("floor", "")).strip().lstrip("-").isdigit()
                    else 999999
                ),
                str(item[1].get("name", "")).strip().lower()
                or str(item[0]).strip().lower(),
            )
        )

        for row_index, (dept_id, dept) in enumerate(valid_departments):
            item = QListWidgetItem(self._department_label(dept))
            item.setData(Qt.UserRole, dept_id)
            self.department_list.addItem(item)

            if dept_id == current_dept_id:
                selected_row = row_index

        self.department_list.blockSignals(False)

        if self.department_list.count() > 0:
            self.department_list.setCurrentRow(selected_row)
            current = self.department_list.currentItem()
            self.current_department_id = current.data(Qt.UserRole) if current else ""
            self.current_department_id = self.current_department_id or ""
        else:
            self.current_department_id = ""

    def configure_multiple_departments(self):
        if not self.current_key:
            QMessageBox.information(
                self,
                "Configure departments",
                "Select a category first.",
            )
            return

        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        dialog = BulkDepartmentTaskGenerationDialog(
            self,
            category_key=self.current_key,
            category_label=(
                self.category_list.currentItem().text()
                if self.category_list.currentItem()
                else self.current_key
            ),
            departments=self.departments,
            base_category=self.config.get("categories", {}).get(self.current_key, {}),
            location_names=self.location_names,
            payload_names=self.payload_names,
            profile_names=self.profile_names,
        )

        if dialog.exec() == QDialog.Accepted and dialog.result:
            category = self.config.setdefault("categories", {}).setdefault(
                self.current_key, {}
            )
            overrides = category.setdefault("departments", {})

            result_dept_ids = sorted(dialog.result.keys())
            result_payload = next(iter(dialog.result.values()))

            for dept_id, payload in dialog.result.items():
                overrides[dept_id] = payload

            self._upsert_department_group(
                self.current_key,
                group_id="",
                department_ids=result_dept_ids,
                payload=result_payload,
            )

            self._load_category(self.current_key)

    def _on_department_changed(self, current, previous):
        if self._loading:
            return

        if previous is not None and self.current_key:
            previous_dept_id = str(previous.data(Qt.UserRole) or "").strip()

            if previous_dept_id and self._current_form_has_generation_settings():
                try:
                    self._store_category(
                        self.current_key,
                        list_item=None,
                        department_id=previous_dept_id,
                    )
                except Exception as exc:
                    self._loading = True
                    self.department_list.setCurrentItem(previous)
                    self._loading = False
                    QMessageBox.critical(self, "Invalid department settings", str(exc))
                    return

        self.current_department_id = current.data(Qt.UserRole) if current else ""
        self.current_department_id = self.current_department_id or ""

        if self.current_key:
            self._loading = True
            self._load_category(self.current_key)
            self._loading = False

    def _department_overrides_for_category(self, category_key):
        category = self.config.setdefault("categories", {}).setdefault(category_key, {})
        overrides = category.setdefault("departments", {})

        if not isinstance(overrides, dict):
            overrides = {}
            category["departments"] = overrides

        return overrides

    def _effective_category_item(self, category_key, department_id=None):
        category = dict(self.config.get("categories", {}).get(category_key, {}))

        if department_id:
            overrides = self._department_overrides_for_category(category_key)
            dept_cfg = overrides.get(department_id, {})
            if isinstance(dept_cfg, dict):
                merged = dict(category)
                merged.update(dept_cfg)
                merged["departments"] = category.get("departments", {})
                return merged

        return category

    def edit_scheduled_times(self):
        dialog = ScheduledTimesDialog(self, self.scheduled_times)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.scheduled_times = list(dialog.result)
            self._refresh_schedule_summary()

    def clear_scheduled_times(self):
        self.scheduled_times = []
        self._refresh_schedule_summary()

    def _refresh_schedule_summary(self):
        if not self.scheduled_times:
            self.schedule_summary.setText("No times selected")
        elif len(self.scheduled_times) <= 8:
            self.schedule_summary.setText(", ".join(self.scheduled_times))
        else:
            self.schedule_summary.setText(
                f"{len(self.scheduled_times)} times selected: "
                + ", ".join(self.scheduled_times[:8])
                + "..."
            )

    def _category_label_pairs(self):
        labels = {key: label for key, label in self.CATEGORY_LABELS}
        pairs = []
        for key, item in self.config.get("categories", {}).items():
            display = str(item.get("display_name", "")).strip() or labels.get(
                key, key.title()
            )
            pairs.append((key, display))
        return sorted(pairs, key=lambda pair: pair[1].lower())

    def _refresh_category_list(self, select_key=None):
        current_key = select_key or self.current_key
        self.category_list.blockSignals(True)
        self.category_list.clear()
        selected_row = 0
        for row, (key, label) in enumerate(self._category_label_pairs()):
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.category_list.addItem(item)
            if key == current_key:
                selected_row = row
        self.category_list.blockSignals(False)
        if self.category_list.count() > 0:
            self.category_list.setCurrentRow(selected_row)
            current = self.category_list.currentItem()
            if current is not None:
                self.current_key = current.data(Qt.UserRole)
                self._load_category(self.current_key)

    def _slugify_category_key(self, value):
        text = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value).strip())
        text = "_".join(part for part in text.split("_") if part)
        return text or "category"

    def add_category(self):
        try:
            self._store_current_category()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid current category", str(exc))
            return

        name, ok = QInputDialog.getText(
            self, "New logistics category", "Category name:"
        )
        if not ok or not name.strip():
            return

        base_key = self._slugify_category_key(name)
        key = base_key
        counter = 2
        while key in self.config.setdefault("categories", {}):
            key = f"{base_key}_{counter}"
            counter += 1

        self.config["categories"][key] = self._default_category(key, name.strip())
        self.current_key = key
        self._refresh_category_list(select_key=key)

    def delete_current_category(self):
        item = self.category_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.UserRole)
        label = item.text()
        if key == "waste":
            QMessageBox.critical(
                self,
                "Delete category",
                "The Waste category cannot be deleted because it is used by the waste-stream task generator.",
            )
            return
        if (
            QMessageBox.question(
                self,
                "Delete category",
                f"Delete logistics category '{label}'?",
            )
            != QMessageBox.Yes
        ):
            return
        self.config.setdefault("categories", {}).pop(key, None)
        self.current_key = None
        self._refresh_category_list()

    def pick_dropoff_locations(self):
        picker = MultiSelectPicker(
            self,
            "Select drop-off destinations",
            self.location_names,
            selected=self.selected_dropoffs,
            group_resolver=lambda item: "Locations",
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_dropoffs = sorted(picker.result)
            self._refresh_dropoff_summary()

    def clear_dropoff_locations(self):
        self.selected_dropoffs = []
        self._refresh_dropoff_summary()

    def _refresh_dropoff_summary(self):
        if not self.selected_dropoffs:
            self.dropoff_summary.setText("None selected")
        elif len(self.selected_dropoffs) <= 4:
            self.dropoff_summary.setText(", ".join(self.selected_dropoffs))
        else:
            self.dropoff_summary.setText(f"{len(self.selected_dropoffs)} selected")

    def _default_category(self, key, label):
        return {
            "enabled": key == "waste",
            "display_name": label,
            "generation_mode": (
                "threshold" if key in {"waste", "linen"} else "scheduled"
            ),
            "uses_department_waste_streams": key == "waste",
            "priority": {
                "catering": 40,
                "pharmacy": 30,
                "linen": 55,
                "waste": 60,
                "stores": 70,
                "ssd": 35,
            }.get(key, 100),
            "pickup_location": "",
            "dropoff_location": "",
            "dropoff_locations": [],
            "payload": "",
            "return_enabled": key in {"catering", "linen", "waste", "ssd"},
            "return_payload": "",
            "requires_staff": key == "stores",
            "staff_handling_minutes": 15.0,
            "staff_initial_count": 1,
            "staff_resource_name": "",
            "staff_movement_policy": (
                "minimise_movement" if key == "catering" else "batch_same_location"
            ),
            "staff_shift_pattern": "none",
            "staff_use_custom_working_hours": False,
            "staff_working_hours": {},
            "route_profile": "",
            "days_active": ["mon", "tue", "wed", "thu", "fri"],
            "schedule_times": {
                "catering": ["07:30", "11:45", "16:45"],
                "pharmacy": ["10:00", "15:00"],
                "stores": ["09:30", "14:30"],
                "ssd": ["08:00", "12:00", "17:00"],
            }.get(key, []),
            "frequency_per_day": 0.0,
            "volume_per_event_m3": 0.0,
            "threshold_volume_m3": 0.0,
            "base_daily_volume_m3": 0.0,
            "timeframe_start": "09:00",
            "timeframe_end": "17:00",
            "timeframe_payload_multiple": 1,
            "payload_multiple": 1,
            "notes": "",
        }

    def _default_global_staff_config(self):
        return GlobalStaffConfigDialog._normalise({})

    def _normalise_global_staff_config(self, value):
        return GlobalStaffConfigDialog._normalise(value)

    def edit_global_staff_config(self):
        dialog = GlobalStaffConfigDialog(self, self.staff_config)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.staff_config = self._normalise_global_staff_config(dialog.result)
            self.config["staff_config"] = json.loads(json.dumps(self.staff_config))
            self._refresh_global_staff_summary()

    def _refresh_global_staff_summary(self):
        cfg = self._normalise_global_staff_config(
            getattr(self, "staff_config", self._default_global_staff_config())
        )
        if not cfg.get("enabled", True):
            self.global_staff_summary.setText("Global staff patterns disabled")
            return
        patterns = cfg.get("shift_patterns", {}) or {}
        fixed = patterns.get("none", {}) or {}
        rotating = patterns.get("four_on_four_off_12h", {}) or {}
        fixed_days = ", ".join(
            str(x).title() for x in fixed.get("days_active", [])
        )
        spacing = (
            "timeframe tasks evenly spaced"
            if cfg.get("spread_timeframe_tasks", True)
            else "timeframe task spacing disabled"
        )
        self.global_staff_summary.setText(
            f"Staff: {fixed.get('start_time', '09:00')}-{fixed.get('end_time', '17:00')} "
            f"({fixed_days}); 4-on/4-off {rotating.get('start_time', '07:00')}-"
            f"{rotating.get('end_time', '19:00')}; {spacing}; "
            f"walking {cfg.get('walking_speed_m_per_sec', 1.2):g} m/s, "
            f"lift wait {cfg.get('lift_wait_seconds', 30.0):g}s."
        )

    def _normalise_config(self, task_generation):
        source = dict(task_generation or {})
        result = {
            "enabled": bool(source.get("enabled", True)),
            "staff_config": self._normalise_global_staff_config(
                source.get("staff_config", source.get("staff", {}))
            ),
            "categories": {},
        }
        incoming_categories = (
            source.get("categories", {})
            if isinstance(source.get("categories", {}), dict)
            else {}
        )
        for key, label in self.CATEGORY_LABELS:
            item = self._default_category(key, label)
            if isinstance(incoming_categories.get(key), dict):
                item.update(incoming_categories[key])
            if isinstance(source.get(key), dict):
                item.update(source[key])
            item["requires_staff"] = bool(
                item.get("requires_staff", item.get("staff_required", False))
            )
            try:
                item["staff_initial_count"] = max(
                    1, int(float(item.get("staff_initial_count", 1) or 1))
                )
            except Exception:
                item["staff_initial_count"] = 1
            item.setdefault("staff_resource_name", "")
            self._normalise_staff_movement_policy(item)
            self._normalise_staff_shift_pattern(item)
            self._normalise_category_staff_hours(item)
            self._normalise_category_dropoffs(item)
            result["categories"][key] = item

        for key, incoming in incoming_categories.items():
            if key in result["categories"] or not isinstance(incoming, dict):
                continue
            item = self._default_category(
                key, str(incoming.get("display_name", key.title()))
            )
            item.update(incoming)
            item["requires_staff"] = bool(
                item.get("requires_staff", item.get("staff_required", False))
            )
            try:
                item["staff_initial_count"] = max(
                    1, int(float(item.get("staff_initial_count", 1) or 1))
                )
            except Exception:
                item["staff_initial_count"] = 1
            item.setdefault("staff_resource_name", "")
            self._normalise_staff_movement_policy(item)
            self._normalise_staff_shift_pattern(item)
            self._normalise_category_staff_hours(item)
            self._normalise_category_dropoffs(item)
            result["categories"][key] = item

        department_waste = dict(source.get("department_waste", {}) or {})
        if department_waste:
            result["categories"]["waste"]["enabled"] = bool(
                department_waste.get(
                    "enabled", result["categories"]["waste"].get("enabled", True)
                )
            )
            result["categories"]["waste"]["priority"] = int(
                float(
                    department_waste.get(
                        "priority", result["categories"]["waste"].get("priority", 60)
                    )
                )
            )
        result["department_waste"] = {
            "enabled": bool(result["categories"]["waste"].get("enabled", True)),
            "priority": int(float(result["categories"]["waste"].get("priority", 60))),
        }
        return result

    def _normalise_category_staff_hours(self, item):
        try:
            item["staff_handling_minutes"] = max(
                0.0, float(item.get("staff_handling_minutes", 15.0) or 0.0)
            )
        except Exception:
            item["staff_handling_minutes"] = 15.0
        item["staff_use_custom_working_hours"] = bool(
            item.get("staff_use_custom_working_hours", False)
        )
        item["staff_working_hours"] = _normalise_staff_weekly_hours(
            item.get("staff_working_hours", {})
        )

    def _normalise_category_dropoffs(self, item):
        selected = item.get("dropoff_locations")
        if isinstance(selected, list):
            locations = [str(x).strip() for x in selected if str(x).strip()]
        else:
            locations = []
        legacy = str(item.get("dropoff_location", "")).strip()
        if legacy and legacy not in locations:
            locations.insert(0, legacy)
        item["dropoff_locations"] = locations
        item["dropoff_location"] = locations[0] if locations else legacy

    def _normalise_staff_movement_policy(self, item):
        policy = str(item.get("staff_movement_policy", "") or "").strip().lower()
        if policy == "minimize_movement":
            policy = "minimise_movement"
        if policy not in {"available_first", "batch_same_location", "minimise_movement"}:
            policy = "batch_same_location"
        item["staff_movement_policy"] = policy

    def _normalise_staff_shift_pattern(self, item):
        item["staff_shift_pattern"] = _normalise_staff_shift_pattern_value(
            item.get("staff_shift_pattern", "none")
        )

    def _on_category_changed(self, current, previous):
        if self._loading:
            return

        # currentItemChanged is emitted after QListWidget has already moved the
        # selection.  Using currentItem() inside the save routine therefore
        # renames the newly selected list row with the previous category name.
        # Save the form into the previous category key and update the previous
        # list item only.
        if previous is not None:
            previous_key = previous.data(Qt.UserRole)
            try:
                self._store_category(
                    previous_key,
                    list_item=previous,
                    department_id=self.current_department_id or "",
                )
            except Exception as exc:
                self._loading = True
                self.category_list.setCurrentItem(previous)
                self._loading = False
                QMessageBox.critical(self, "Invalid category", str(exc))
                return

        if current is None:
            self.current_key = None
            return

        self.current_key = current.data(Qt.UserRole)

        self._loading = True
        self._refresh_department_list(select_dept_id=self.current_department_id)

        if self.department_list.count() > 0:
            current_dept = self.department_list.currentItem()
            self.current_department_id = (
                current_dept.data(Qt.UserRole) if current_dept else ""
            )
            self.current_department_id = self.current_department_id or ""
            self._load_category(self.current_key)
        else:
            self.current_department_id = ""
            self._clear_task_generation_form()

        self._loading = False

    def _clear_task_generation_form(self):
        self.enabled_check.setChecked(False)
        self.display_name_edit.setText("")
        self.display_name_edit.setEnabled(False)
        self.mode_combo.setCurrentText("scheduled")
        self.priority_edit.setText("100")
        self.pickup_combo.setCurrentText("")
        self.selected_dropoffs = []
        self._refresh_dropoff_summary()
        self.payload_combo.setCurrentText("")
        self.delivery_resource_combo.setCurrentIndex(
            max(0, self.delivery_resource_combo.findData("amr"))
        )
        self.route_profile_combo.setCurrentText("")
        self.return_enabled_check.setChecked(False)
        self.return_payload_combo.setCurrentText("")
        self.return_delay_edit.setText("0")
        self.requires_staff_check.setChecked(False)
        self.staff_initial_count_edit.setText("1")
        self.staff_resource_name_edit.setText("")
        self._set_staff_movement_policy("batch_same_location")
        _set_staff_shift_pattern_combo(self.staff_shift_pattern_combo, "none")
        self.reusable_return_pool_check.setChecked(False)
        self.reusable_return_pool_multiplier_edit.setText("2.0")
        self.reusable_return_pool_max_edit.setText("0")

        if hasattr(self, "tracked_item_exchange_check"):
            self.tracked_item_exchange_check.setChecked(False)

        if hasattr(self, "exchange_mode_combo"):
            self.exchange_mode_combo.setCurrentText("top_up_only")

        for day_key, _label in self.DAYS:
            self.day_checks[day_key].setChecked(False)

        self.run_every_fortnight_check.setChecked(False)
        self.scheduled_times = []
        self._refresh_schedule_summary()

        self.frequency_edit.setText("0.0")
        self.volume_per_event_edit.setText("0.0")
        self.threshold_volume_edit.setText("0.0")
        self.base_daily_volume_edit.setText("0.0")
        self.timeframe_start_edit.setText("")
        self.timeframe_end_edit.setText("")
        self.timeframe_payload_multiple_edit.setText("1")
        self.notes_edit.setPlainText("")
        self._set_department_location_role("dropoff", True)
        self._update_mode_field_state()

    def _is_waste_category(self):
        return str(self.current_key or "").strip().lower() == "waste"

    def _set_department_location_role(self, role, checked):
        if not checked:
            return

        role = "pickup" if str(role).strip() == "pickup" else "dropoff"

        self.role_pickup_radio.blockSignals(True)
        self.role_dropoff_radio.blockSignals(True)
        self.role_pickup_radio.setChecked(role == "pickup")
        self.role_dropoff_radio.setChecked(role == "dropoff")
        self.role_pickup_radio.blockSignals(False)
        self.role_dropoff_radio.blockSignals(False)

        self._update_mode_field_state()

    def _current_department_location_role(self):
        return "pickup" if self.role_pickup_radio.isChecked() else "dropoff"

    def _load_category(self, key):
        was_loading = self._loading
        self._loading = True
        item = self._effective_category_item(key, self.current_department_id)
        self._normalise_category_dropoffs(item)
        self.selected_dropoffs = list(item.get("dropoff_locations", []))
        self.enabled_check.setChecked(bool(item.get("enabled", False)))

        category = self.config.get("categories", {}).get(key, {})
        self.display_name_edit.setText(str(category.get("display_name", key.title())))
        self.display_name_edit.setEnabled(False)

        self.mode_combo.setCurrentText(str(item.get("generation_mode", "scheduled")))
        self.priority_edit.setText(str(item.get("priority", 100)))
        role = str(item.get("department_location_role", "dropoff") or "dropoff").strip()
        self._set_department_location_role(
            "pickup" if role == "pickup" else "dropoff", True
        )
        self.pickup_combo.setCurrentText(str(item.get("pickup_location", "")))
        self._refresh_dropoff_summary()
        self.payload_combo.setCurrentText(str(item.get("payload", "")))
        delivery_policy = item.get(
            "delivery_resource", item.get("delivery_method", "amr")
        )
        delivery_mode = normalise_delivery_resource_policy(delivery_policy).get(
            "mode", "amr"
        )
        self.delivery_resource_combo.setCurrentIndex(
            max(0, self.delivery_resource_combo.findData(delivery_mode))
        )
        self.tracked_item_exchange_check.setChecked(
            bool(item.get("tracked_item_exchange", False))
        )
        self.exchange_mode_combo.setCurrentText(
            str(item.get("exchange_mode", "top_up_only"))
        )
        self.route_profile_combo.setCurrentText(str(item.get("route_profile", "")))
        self.return_enabled_check.setChecked(bool(item.get("return_enabled", False)))
        self.return_payload_combo.setCurrentText(str(item.get("return_payload", "")))
        self.return_delay_edit.setText(str(item.get("return_delay_minutes", 0)))
        self.staff_handling_minutes_edit.setText(
            str(item.get("staff_handling_minutes", 15.0))
        )
        self.requires_staff_check.setChecked(
            bool(item.get("requires_staff", item.get("staff_required", False)))
        )
        self.staff_initial_count_edit.setText(str(item.get("staff_initial_count", 1)))
        self.staff_resource_name_edit.setText(str(item.get("staff_resource_name", "")))
        self._set_staff_movement_policy(
            item.get("staff_movement_policy", "batch_same_location")
        )
        _set_staff_shift_pattern_combo(
            self.staff_shift_pattern_combo,
            item.get("staff_shift_pattern", "none"),
        )
        self.staff_use_custom_working_hours = bool(
            item.get("staff_use_custom_working_hours", False)
        )
        self.staff_working_hours = _normalise_staff_weekly_hours(
            item.get("staff_working_hours", {})
        )
        self._refresh_staff_working_hours_summary()
        self.reusable_return_pool_check.setChecked(bool(item.get("reusable_return_pool_enabled", False)))
        self.reusable_return_pool_multiplier_edit.setText(str(item.get("reusable_return_pool_multiplier", 2.0)))
        self.reusable_return_pool_max_edit.setText(str(item.get("reusable_return_pool_max", 0)))
        days = set(item.get("days_active", []))
        for day_key, _label in self.DAYS:
            self.day_checks[day_key].setChecked(day_key in days)
        self.run_every_fortnight_check.setChecked(bool(item.get("run_every_fortnight", False)))
        self.scheduled_times = list(item.get("scheduled_times", []))

        legacy_schedule = str(item.get("schedule", "")).strip()
        if legacy_schedule and not self.scheduled_times:
            self.scheduled_times = [
                x.strip() for x in legacy_schedule.split(",") if x.strip()
            ]

        self._refresh_schedule_summary()
        self.frequency_edit.setText(str(item.get("frequency_per_day", 0.0)))
        self.volume_per_event_edit.setText(str(item.get("volume_per_event_m3", 0.0)))
        self.threshold_volume_edit.setText(str(item.get("threshold_volume_m3", 0.0)))
        self.base_daily_volume_edit.setText(str(item.get("base_daily_volume_m3", 0.0)))
        self.timeframe_start_edit.setText(str(item.get("timeframe_start", "")))
        self.timeframe_end_edit.setText(str(item.get("timeframe_end", "")))
        self.timeframe_payload_multiple_edit.setText(str(item.get("timeframe_payload_multiple", item.get("payload_multiple", 1))))
        self.notes_edit.setPlainText(str(item.get("notes", "")))
        self._loading = was_loading
        self._update_mode_field_state()

    def _set_widget_enabled(self, widget, enabled):
        widget.setEnabled(bool(enabled))

    def _set_form_row_visible(self, field_widget, visible):
        visible = bool(visible)
        if field_widget is None:
            return
        try:
            field_widget.setVisible(visible)
        except Exception:
            pass
        try:
            label = self.form.labelForField(field_widget)
            if label is not None:
                label.setVisible(visible)
        except Exception:
            pass

    def _update_mode_field_state(self, *_):
        mode = self.mode_combo.currentText().strip()
        is_waste = self._is_waste_category()

        uses_schedule = mode in {
            "scheduled",
            "scheduled_threshold",
            "scheduled_sporadic",
        }

        uses_threshold = mode in {
            "threshold",
            "hybrid",
            "scheduled_threshold",
        }

        uses_continuous = mode in {
            "continuous",
            "hybrid",
        }

        uses_sporadic = mode in {
            "sporadic",
            "hybrid",
            "scheduled_sporadic",
        }

        uses_timeframe = mode == "timeframe"

        # Waste stream generation values are now edited on the Department
        # waste-stream assignments.  The Waste category only controls routing,
        # collection/destination and return-task behaviour.
        self.waste_stream_notice_label.setVisible(is_waste)

        self.mode_combo.setEnabled(not is_waste)
        self.payload_combo.setEnabled(not is_waste)
        self.tracked_item_exchange_check.setEnabled(not is_waste)
        show_exchange_mode = (not is_waste) and self.tracked_item_exchange_check.isChecked()
        self.exchange_mode_combo.setEnabled(show_exchange_mode)
        self._set_form_row_visible(self.exchange_mode_combo, show_exchange_mode)

        show_generation = not is_waste

        self._set_form_row_visible(self.schedule_widget, show_generation and uses_schedule)
        self._set_form_row_visible(self.run_every_fortnight_check, show_generation)
        self._set_form_row_visible(self.threshold_volume_edit, show_generation and uses_threshold)
        self._set_form_row_visible(
            self.base_daily_volume_edit,
            show_generation and (uses_continuous or uses_threshold),
        )
        self._set_form_row_visible(self.frequency_edit, show_generation and uses_sporadic)
        self._set_form_row_visible(self.volume_per_event_edit, show_generation and uses_sporadic)
        self._set_form_row_visible(self.timeframe_start_edit, show_generation and uses_timeframe)
        self._set_form_row_visible(self.timeframe_end_edit, show_generation and uses_timeframe)
        self._set_form_row_visible(self.timeframe_payload_multiple_edit, show_generation and uses_timeframe)

        self.schedule_summary.setEnabled(show_generation and uses_schedule)
        self.schedule_button.setEnabled(show_generation and uses_schedule)
        self.clear_schedule_button.setEnabled(show_generation and uses_schedule)

        using_dept_as_pickup = self._current_department_location_role() == "pickup"
        self.pickup_combo.setEnabled(not using_dept_as_pickup)
        self.dropoff_summary.setEnabled(using_dept_as_pickup)
        self.pick_dropoffs_btn.setEnabled(using_dept_as_pickup)
        self.clear_dropoffs_btn.setEnabled(using_dept_as_pickup)

        return_enabled = self.return_enabled_check.isChecked()
        staff_enabled = show_generation and self.requires_staff_check.isChecked()
        pool_enabled = return_enabled and self.reusable_return_pool_check.isChecked()
        self._set_form_row_visible(self.return_payload_combo, return_enabled)
        self._set_form_row_visible(self.return_delay_edit, return_enabled)
        self._set_form_row_visible(self.requires_staff_check, show_generation)
        self._set_form_row_visible(self.staff_handling_minutes_edit, staff_enabled)
        self._set_form_row_visible(self.staff_initial_count_edit, staff_enabled)
        self._set_form_row_visible(self.staff_resource_name_edit, staff_enabled)
        self._set_form_row_visible(self.staff_movement_policy_widget, staff_enabled)
        self._set_form_row_visible(self.staff_shift_pattern_combo, staff_enabled)
        self._set_form_row_visible(self.staff_hours_widget, staff_enabled)
        self.requires_staff_check.setEnabled(show_generation)
        self.staff_handling_minutes_edit.setEnabled(staff_enabled)
        self.staff_initial_count_edit.setEnabled(staff_enabled)
        self.staff_resource_name_edit.setEnabled(staff_enabled)
        self.staff_movement_policy_widget.setEnabled(staff_enabled)
        self.staff_shift_pattern_combo.setEnabled(staff_enabled)
        self.staff_hours_widget.setEnabled(staff_enabled)
        if hasattr(self, "reusable_return_pool_check"):
            self._set_form_row_visible(self.reusable_return_pool_check, return_enabled)
            self._set_form_row_visible(self.reusable_return_pool_multiplier_edit, pool_enabled)
            self._set_form_row_visible(self.reusable_return_pool_max_edit, pool_enabled)
            self.reusable_return_pool_check.setEnabled(return_enabled)
            self.reusable_return_pool_multiplier_edit.setEnabled(pool_enabled)
            self.reusable_return_pool_max_edit.setEnabled(pool_enabled)

    def _normalise_hhmm_text(self, value):
        """Return a clean HH:MM value, or blank for empty/masked time edits."""
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.replace("_", "").replace(" ", "")
        if text in {"", ":"}:
            return ""
        return text

    def _float_from_edit(self, widget, default=0.0):
        text = str(widget.text() if widget is not None else "").strip()
        if not text:
            return float(default)
        text = text.replace("_", "").strip()
        if not text:
            return float(default)
        return float(text)

    def _int_from_edit(self, widget, default=0):
        return int(float(self._float_from_edit(widget, default)))

    def _parse_timeframe_hhmm(self, value, field_name):
        text = str(value or "").strip()
        try:
            parts = text.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if hour == 24 and minute == 0:
                return 24 * 60
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour * 60) + minute
        except Exception:
            pass
        raise ValueError(f"{field_name} must be HH:MM, for example 09:00")

    def _store_current_category(self):
        if not self.current_key:
            return

        if not self.current_department_id:
            return

        # Always pass through to _store_category.  A blank form means the user
        # has intentionally cleared this department override, so the old override
        # must be removed rather than left in the JSON.
        self._store_category(
            self.current_key,
            list_item=None,
            department_id=self.current_department_id,
        )

    def _store_category(self, category_key, list_item=None, department_id=""):
        if not category_key:
            return

        department_id = str(department_id or "").strip()

        if not department_id:
            return

        category = self.config.setdefault("categories", {}).setdefault(category_key, {})
        overrides = category.setdefault("departments", {})

        # A fully blank/disabled form is a valid clear operation.  Remove the
        # department override before validating days, timeframe or numeric fields
        # so empty cleared values do not block saving.
        if not self._current_form_has_generation_settings():
            overrides.pop(department_id, None)
            self._remove_departments_from_groups(category_key, [department_id])
            return

        days_active = [
            key for key, _label in self.DAYS if self.day_checks[key].isChecked()
        ]
        if self.enabled_check.isChecked() and not days_active:
            raise ValueError("Select at least one active day")

        timeframe_start = self._normalise_hhmm_text(self.timeframe_start_edit.text())
        timeframe_end = self._normalise_hhmm_text(self.timeframe_end_edit.text())
        timeframe_multiple = max(1, self._int_from_edit(self.timeframe_payload_multiple_edit, 1))

        if self.mode_combo.currentText().strip() == "timeframe" and self.enabled_check.isChecked():
            if timeframe_start:
                self._parse_timeframe_hhmm(timeframe_start, "Timeframe start")
            if timeframe_end:
                self._parse_timeframe_hhmm(timeframe_end, "Timeframe end")

        display_name = (
            self.display_name_edit.text().strip() or str(category_key).title()
        )
        dropoff_locations = [
            str(x).strip() for x in self.selected_dropoffs if str(x).strip()
        ]
        payload = {
            "enabled": self.enabled_check.isChecked(),
            "display_name": display_name,
            "generation_mode": self.mode_combo.currentText().strip(),
            "priority": self._int_from_edit(self.priority_edit, 100),
            "department_location_role": self._current_department_location_role(),
            "pickup_location": self.pickup_combo.currentText().strip(),
            "dropoff_location": dropoff_locations[0] if dropoff_locations else "",
            "dropoff_locations": dropoff_locations,
            "payload": self.payload_combo.currentText().strip(),
            "delivery_resource": normalise_delivery_resource_policy(
                {"mode": self.delivery_resource_combo.currentData()}
            ),
            "tracked_item_exchange": self.tracked_item_exchange_check.isChecked(),
            "exchange_mode": self.exchange_mode_combo.currentText().strip(),
            "return_enabled": self.return_enabled_check.isChecked(),
            "return_payload": self.return_payload_combo.currentText().strip(),
            "return_delay_minutes": self._float_from_edit(self.return_delay_edit, 0.0),
            "requires_staff": self.requires_staff_check.isChecked(),
            "staff_handling_minutes": max(
                0.0, self._float_from_edit(self.staff_handling_minutes_edit, 15.0)
            ),
            "staff_initial_count": max(1, self._int_from_edit(self.staff_initial_count_edit, 1)),
            "staff_resource_name": self.staff_resource_name_edit.text().strip(),
            "staff_movement_policy": self._selected_staff_movement_policy(),
            "staff_shift_pattern": _selected_staff_shift_pattern_combo(
                self.staff_shift_pattern_combo
            ),
            "staff_use_custom_working_hours": bool(
                self.staff_use_custom_working_hours
            ),
            "staff_working_hours": _normalise_staff_weekly_hours(
                self.staff_working_hours
            ),
            "reusable_return_pool_enabled": self.reusable_return_pool_check.isChecked(),
            "reusable_return_pool_multiplier": self._float_from_edit(self.reusable_return_pool_multiplier_edit, 2.0),
            "reusable_return_pool_max": self._int_from_edit(self.reusable_return_pool_max_edit, 0),
            "route_profile": self.route_profile_combo.currentText().strip(),
            "days_active": days_active,
            "run_every_fortnight": self.run_every_fortnight_check.isChecked(),
            "scheduled_times": list(self.scheduled_times),
            "frequency_per_day": self._float_from_edit(self.frequency_edit, 0.0),
            "volume_per_event_m3": self._float_from_edit(self.volume_per_event_edit, 0.0),
            "threshold_volume_m3": self._float_from_edit(self.threshold_volume_edit, 0.0),
            "base_daily_volume_m3": self._float_from_edit(self.base_daily_volume_edit, 0.0),
            "timeframe_start": timeframe_start,
            "timeframe_end": timeframe_end,
            "timeframe_payload_multiple": timeframe_multiple,
            "payload_multiple": timeframe_multiple,
            "notes": self.notes_edit.toPlainText().strip(),
        }

        if str(category_key).strip().lower() == "waste":
            # Waste generation mode, volumes and stream payloads now come from
            # departments[].waste_streams[] and the global waste_streams list.
            # Keep the category override focused on task-generator routing and
            # collection metadata only.
            payload["generation_mode"] = "threshold"
            payload["payload"] = ""
            payload["tracked_item_exchange"] = False
            payload["exchange_mode"] = "top_up_only"
            payload["scheduled_times"] = []
            payload["run_every_fortnight"] = False
            payload["frequency_per_day"] = 0.0
            payload["volume_per_event_m3"] = 0.0
            payload["threshold_volume_m3"] = 0.0
            payload["base_daily_volume_m3"] = 0.0
            payload["timeframe_start"] = ""
            payload["timeframe_end"] = ""
            payload["timeframe_payload_multiple"] = 1
            payload["payload_multiple"] = 1

        payload.pop("display_name", None)
        payload.pop("departments", None)

        # If the department row is effectively blank, remove the override instead
        # of writing disabled/default config into the JSON.
        if not self._current_form_has_generation_settings():
            overrides.pop(department_id, None)
            self._remove_departments_from_groups(category_key, [department_id])
            return

        # Also remove empty disabled overrides. This prevents switching departments
        # from creating blank/default task_generation entries.
        if not payload.get("enabled", False):
            has_meaningful_disabled_config = any(
                [
                    payload.get("department_location_role")
                    not in {"", "dropoff", None},
                    payload.get("pickup_location"),
                    payload.get("dropoff_location"),
                    payload.get("dropoff_locations"),
                    payload.get("payload"),
                    payload.get("return_enabled"),
                    payload.get("return_payload"),
                    payload.get("requires_staff"),
                    int(float(payload.get("staff_initial_count", 1) or 1)) != 1,
                    payload.get("staff_resource_name"),
                    payload.get("route_profile"),
                    payload.get("scheduled_times"),
                    float(payload.get("frequency_per_day", 0.0) or 0.0) != 0.0,
                    float(payload.get("volume_per_event_m3", 0.0) or 0.0) != 0.0,
                    float(payload.get("threshold_volume_m3", 0.0) or 0.0) != 0.0,
                    float(payload.get("base_daily_volume_m3", 0.0) or 0.0) != 0.0,
                    payload.get("timeframe_start"),
                    payload.get("timeframe_end"),
                    int(float(payload.get("timeframe_payload_multiple", payload.get("payload_multiple", 1)) or 1)) != 1,
                    payload.get("notes"),
                    payload.get("tracked_item_exchange"),
                ]
            )

            if not has_meaningful_disabled_config:
                overrides.pop(department_id, None)
                self._remove_departments_from_groups(category_key, [department_id])
                return

        overrides[department_id] = payload
        self._remove_department_from_group_if_settings_changed(
            category_key,
            department_id,
            payload,
        )

        if list_item is not None and not department_id:
            list_item.setText(display_name)

    def accept(self):
        try:
            self._store_current_category()
            self.config["enabled"] = self.global_enabled.isChecked()
            self.config["staff_config"] = self._normalise_global_staff_config(
                self.staff_config
            )
            waste = self.config["categories"].get("waste", {})
            self.config["department_waste"] = {
                "enabled": bool(waste.get("enabled", True)),
                "priority": int(float(waste.get("priority", 60) or 0)),
            }
            self.on_save(self.config)
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid task generation settings", str(exc))


class PointEditorDialog(QDialog):
    """Edit a topology point using typed controls and context-sensitive options."""

    def __init__(self, parent, title, point_name, point):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.point_name = point_name
        self.point = point
        self.result = None
        self.parent_obj = self.parent()

        layout = QVBoxLayout(self)
        kind = str(point.get("kind", "") or "").strip()
        friendly_kind = {
            "location": "Location",
            "corridor_node": "Corridor node",
            "department": "Department location",
        }.get(kind, kind.replace("_", " ").title() or "Point")
        layout.addWidget(
            _dialog_intro(
                f"Edit this {friendly_kind.lower()}. Coordinates use drawing metres. "
                "Operational settings are shown only when they apply to this point type."
            )
        )

        identity_box = QGroupBox("Identity and position")
        identity_form = QFormLayout(identity_box)
        self.name_edit = QLineEdit(str(point_name))
        self.name_edit.setPlaceholderText("Enter a unique point name")
        self.x_edit = _double_input(
            point.get("x", 0.0), minimum=-1_000_000.0, maximum=1_000_000.0,
            decimals=3, suffix=" m", step=0.1,
            tooltip="Horizontal drawing coordinate in metres.",
        )
        self.y_edit = _double_input(
            point.get("y", 0.0), minimum=-1_000_000.0, maximum=1_000_000.0,
            decimals=3, suffix=" m", step=0.1,
            tooltip="Vertical drawing coordinate in metres.",
        )
        identity_form.addRow("Name", self.name_edit)
        identity_form.addRow("X coordinate", self.x_edit)
        identity_form.addRow("Y coordinate", self.y_edit)
        identity_form.addRow("Floor", QLabel(str(point.get("floor", 0))))
        identity_form.addRow("Point type", QLabel(friendly_kind))
        layout.addWidget(identity_box)

        if kind == "location":
            metrics = {}
            if self.parent_obj and hasattr(self.parent_obj, "store"):
                metrics = self.parent_obj.store.location_bounding_box_metrics(point_name)
            size_box = QGroupBox("Usable location boundary")
            size_form = QFormLayout(size_box)
            size_form.addRow("Length", QLabel(f"{float(metrics.get('length', 0.0)):.3f} m"))
            size_form.addRow("Width", QLabel(f"{float(metrics.get('width', 0.0)):.3f} m"))
            size_form.addRow("Area", QLabel(f"{float(metrics.get('area', 0.0)):.3f} m²"))
            layout.addWidget(size_box)

            operations_box = QGroupBox("Operational behaviour")
            operations_form = QFormLayout(operations_box)
            self.people_area_combo = QComboBox()
            for label, value in [
                ("No routine occupancy", "none"),
                ("Staff only", "staff"),
                ("Public only", "public"),
                ("Mixed staff and public", "both"),
            ]:
                self.people_area_combo.addItem(label, value)
            area_value = str(point.get("people_area_type", "none") or "none").lower()
            self.people_area_combo.setCurrentIndex(max(0, self.people_area_combo.findData(area_value)))
            self.people_area_combo.setToolTip(
                "Classifies the location for people-routing profiles and route compatibility."
            )

            self.wash_cycle_check = QCheckBox(
                "Require the AMR to complete a wash cycle after visiting this location"
            )
            self.wash_cycle_check.setChecked(bool(point.get("wash_cycle_required", False)))
            self.wash_duration_edit = _double_input(
                point.get("wash_cycle_duration_sec", 300.0), minimum=0.0,
                maximum=86_400.0, decimals=0, suffix=" s", step=30.0,
                tooltip="Time the AMR remains unavailable while the wash cycle is completed.",
            )
            self.wash_location_combo = QComboBox()
            self.wash_location_combo.addItem("Use this location", "")
            location_names = []
            if self.parent_obj and hasattr(self.parent_obj, "store"):
                location_names = sorted(
                    str(item.get("name", "")).strip()
                    for item in self.parent_obj.store.data.get("locations", [])
                    if str(item.get("name", "")).strip()
                )
            for name in location_names:
                self.wash_location_combo.addItem(name, name)
            wash_location = str(point.get("wash_location", "") or "").strip()
            self.wash_location_combo.setCurrentIndex(
                max(0, self.wash_location_combo.findData(wash_location))
            )
            operations_form.addRow("People using this area", self.people_area_combo)
            operations_form.addRow("Infection / hazardous-area control", self.wash_cycle_check)
            operations_form.addRow("Wash-cycle duration", self.wash_duration_edit)
            operations_form.addRow("Wash location", self.wash_location_combo)
            layout.addWidget(operations_box)
            self.wash_cycle_check.toggled.connect(self._update_location_controls)
            self._update_location_controls()

        elif kind == "corridor_node":
            door_box = QGroupBox("Door opening")
            door_form = QFormLayout(door_box)
            self.has_door_check = QCheckBox(
                "This node represents a door or other local width restriction"
            )
            self.has_door_check.setChecked(bool(point.get("has_door", False)))
            self.door_clear_width_edit = _double_input(
                point.get("door_clear_width_m", 0.9), minimum=0.1, maximum=20.0,
                decimals=3, suffix=" m", step=0.05,
                tooltip="Clear usable opening, not the nominal leaf or frame width.",
            )
            door_form.addRow("Restriction", self.has_door_check)
            door_form.addRow("Clear opening width", self.door_clear_width_edit)
            layout.addWidget(door_box)
            layout.addWidget(
                _dialog_intro(
                    "The simulator uses the narrowest width along a corridor. A door opening can therefore "
                    "reduce a wider corridor to single-lane AMR movement."
                )
            )
            self.has_door_check.toggled.connect(self._update_door_controls)
            self._update_door_controls()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def _update_location_controls(self):
        enabled = bool(self.wash_cycle_check.isChecked())
        self.wash_duration_edit.setEnabled(enabled)
        self.wash_location_combo.setEnabled(enabled)

    def _update_door_controls(self):
        self.door_clear_width_edit.setEnabled(bool(self.has_door_check.isChecked()))

    def accept(self):
        try:
            name = self.name_edit.text().strip()
            if not name:
                raise ValueError("Enter a name for this point.")
            self.result = {
                "name": name,
                "x": float(self.x_edit.value()),
                "y": float(self.y_edit.value()),
            }
            if self.point.get("kind") == "location":
                wash_required = bool(self.wash_cycle_check.isChecked())
                wash_duration = float(self.wash_duration_edit.value())
                if wash_required and wash_duration <= 0.0:
                    raise ValueError("Wash-cycle duration must be greater than zero when wash control is enabled.")
                self.result.update(
                    {
                        "wash_cycle_required": wash_required,
                        "wash_cycle_duration_sec": wash_duration,
                        "wash_location": str(self.wash_location_combo.currentData() or "").strip(),
                        "people_area_type": str(self.people_area_combo.currentData() or "none"),
                    }
                )
            elif self.point.get("kind") == "corridor_node":
                self.result.update(
                    {
                        "has_door": bool(self.has_door_check.isChecked()),
                        "door_clear_width_m": float(self.door_clear_width_edit.value()),
                    }
                )
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check point details", str(exc))


class EdgeConnectionsDialog(QDialog):
    columns = [
        ("from", "From", 180),
        ("from_floor", "From floor", 90),
        ("to", "To", 180),
        ("to_floor", "To floor", 90),
        ("cross_floor", "Cross-floor", 90),
    ]

    def __init__(self, parent, point_name, edges, on_delete):
        super().__init__(parent)
        self.setWindowTitle(f"Edge Connections - {point_name}")
        self.resize(760, 420)
        self.point_name = point_name
        self.edges = list(edges)
        self.on_delete = on_delete

        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table, 1)

        button_row = QHBoxLayout()
        layout.addLayout(button_row)
        self.delete_btn = QPushButton("Delete selected")
        close_btn = QPushButton("Close")
        button_row.addWidget(self.delete_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)

        self.delete_btn.clicked.connect(self.delete_selected)
        close_btn.clicked.connect(self.accept)

        self._refresh_table()
        _polish_dialog(self)

    def _refresh_table(self):
        self.table.setRowCount(0)
        for edge in self.edges:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                edge.get("from", ""),
                edge.get("from_floor", ""),
                edge.get("to", ""),
                edge.get("to_floor", ""),
                "Yes" if edge.get("cross_floor") else "No",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        count = len(self.edges)
        if count == 0:
            self.summary_label.setText(f"No edge connections for {self.point_name}")
            self.delete_btn.setEnabled(False)
        else:
            cross_count = sum(1 for edge in self.edges if edge.get("cross_floor"))
            self.summary_label.setText(
                f"{count} connection(s) for {self.point_name} ({cross_count} cross-floor)"
            )
            self.delete_btn.setEnabled(True)

    def delete_selected(self):
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()}
        )
        if not rows:
            QMessageBox.information(
                self, "Delete edges", "Select one or more edge connections first."
            )
            return
        selected_edges = [self.edges[row] for row in rows]
        if (
            QMessageBox.question(
                self,
                "Delete edges",
                f"Delete {len(selected_edges)} selected edge connection(s)?",
            )
            != QMessageBox.Yes
        ):
            return
        self.on_delete(selected_edges)
        for row in reversed(rows):
            del self.edges[row]
        self._refresh_table()


class LiftEditorDialog(QDialog):
    def __init__(
        self, parent, lift=None, default_floor=0, default_x=0.0, default_y=0.0
    ):
        super().__init__(parent)
        self.lift = dict(lift or {})
        self.setWindowTitle("Edit lift" if self.lift else "Add lift")
        self.resize(760, 700)
        self.setMinimumSize(660, 560)
        self.result = None
        self.default_floor = int(default_floor)
        self.default_x = float(default_x)
        self.default_y = float(default_y)

        floors = self.lift.get("served_floors", [self.default_floor])
        self.existing_floor_locations = self._normalise_floor_locations(
            self.lift.get("floor_locations", {})
        )

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Configure the lift car, journey timing and reliability. Values use physical units, "
                "and each lift entrance is generated automatically on every served floor."
            )
        )
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        general_page = QWidget()
        tabs.addTab(general_page, "Lift and floors")
        general_layout = QVBoxLayout(general_page)
        identity_box = QGroupBox("Identity and floor service")
        identity_form = QFormLayout(identity_box)
        default_lift_id = self._suggest_next_lift_id()
        self.id_edit = QLineEdit(str(self.lift.get("id", default_lift_id)))
        self.id_edit.setPlaceholderText("For example, Lift-1")
        self.floors_edit = QLineEdit(", ".join(str(x) for x in floors))
        self.floors_edit.setPlaceholderText("For example, 0, 1, 2, 3")
        self.floors_edit.setToolTip(
            "Comma-separated floor numbers. Existing floor coordinates are retained; new floors use the clicked position."
        )
        self.start_floor_edit = _integer_input(
            self.lift.get("start_floor", self.default_floor),
            minimum=-100, maximum=500,
            tooltip="Floor where the lift starts at the beginning of the simulation.",
        )
        identity_form.addRow("Lift name", self.id_edit)
        identity_form.addRow("Floors served", self.floors_edit)
        identity_form.addRow("Starting floor", self.start_floor_edit)
        general_layout.addWidget(identity_box)

        timing_box = QGroupBox("Journey timing")
        timing_form = QFormLayout(timing_box)
        self.speed_edit = _double_input(
            self._default_speed_m_per_sec(), minimum=0.01, maximum=20.0,
            decimals=3, suffix=" m/s", step=0.1,
            tooltip="Vertical car travel speed. Door and boarding times are added separately.",
        )
        self.door_edit = _double_input(
            self.lift.get("door_time_sec", 4), minimum=0.0, maximum=600.0,
            decimals=1, suffix=" s", step=0.5,
            tooltip="Total opening and closing allowance for one lift stop.",
        )
        self.board_edit = _double_input(
            self.lift.get("boarding_time_sec", 6), minimum=0.0, maximum=600.0,
            decimals=1, suffix=" s", step=0.5,
            tooltip="Time allowed for the AMR to enter or leave the lift car.",
        )
        timing_form.addRow("Vertical travel speed", self.speed_edit)
        timing_form.addRow("Door operation time", self.door_edit)
        timing_form.addRow("AMR boarding / exit time", self.board_edit)
        general_layout.addWidget(timing_box)

        positions_box = QGroupBox("Generated entrance positions")
        positions_layout = QVBoxLayout(positions_box)
        self.positions_edit = QPlainTextEdit()
        self.positions_edit.setReadOnly(True)
        self.positions_edit.setMaximumHeight(150)
        self.positions_edit.setToolTip(
            "Automatically generated from the floors served. Existing positions are retained."
        )
        positions_layout.addWidget(self.positions_edit)
        positions_layout.addWidget(
            QLabel(
                "Existing floor positions are kept. A newly added floor uses the X/Y position where the lift was created."
            )
        )
        general_layout.addWidget(positions_box)
        general_layout.addStretch(1)

        car_page = QWidget()
        tabs.addTab(car_page, "Car and energy")
        car_layout = QVBoxLayout(car_page)
        capacity_box = QGroupBox("Usable car envelope")
        capacity_form = QFormLayout(capacity_box)
        self.capacity_length_edit = _double_input(
            self.lift.get("capacity_length_m", 1.0), minimum=0.1, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
            tooltip="Maximum clear length available to the AMR and its carried payload.",
        )
        self.capacity_width_edit = _double_input(
            self.lift.get("capacity_width_m", 1.0), minimum=0.1, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
            tooltip="Maximum clear width available to the AMR and its carried payload.",
        )
        self.capacity_height_edit = _double_input(
            self.lift.get("capacity_height_m", 2.0), minimum=0.1, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
            tooltip="Maximum clear height available to the AMR and its carried payload.",
        )
        capacity_form.addRow("Clear car length", self.capacity_length_edit)
        capacity_form.addRow("Clear car width", self.capacity_width_edit)
        capacity_form.addRow("Clear car height", self.capacity_height_edit)
        car_layout.addWidget(capacity_box)

        energy_box = QGroupBox("Energy model")
        energy_form = QFormLayout(energy_box)
        self.car_mass_edit = _double_input(
            self.lift.get("car_mass_kg", 1200.0), minimum=1.0, maximum=1_000_000.0,
            decimals=1, suffix=" kg", step=10.0,
        )
        self.counterweight_edit = _double_input(
            self.lift.get("counterweight_ratio", 0.5), minimum=0.0, maximum=2.0,
            decimals=3, step=0.05,
            tooltip="Counterweight mass as a fraction of the lift car mass.",
        )
        self.travel_efficiency_edit = _double_input(
            self.lift.get("travel_efficiency", 0.75), minimum=0.01, maximum=1.0,
            decimals=3, step=0.05,
            tooltip="Electrical-to-mechanical travel efficiency expressed as a fraction.",
        )
        self.door_power_edit = _double_input(
            self.lift.get("door_power_w", 800.0), minimum=0.0, maximum=1_000_000.0,
            decimals=1, suffix=" W", step=10.0,
        )
        self.standby_power_edit = _double_input(
            self.lift.get("standby_power_w", 120.0), minimum=0.0, maximum=1_000_000.0,
            decimals=1, suffix=" W", step=10.0,
        )
        self.regen_efficiency_edit = _double_input(
            self.lift.get("regen_efficiency", 0.2), minimum=0.0, maximum=1.0,
            decimals=3, step=0.05,
            tooltip="Fraction of recoverable travel energy returned through regeneration.",
        )
        energy_form.addRow("Lift car mass", self.car_mass_edit)
        energy_form.addRow("Counterweight ratio", self.counterweight_edit)
        energy_form.addRow("Travel efficiency", self.travel_efficiency_edit)
        energy_form.addRow("Door-system power", self.door_power_edit)
        energy_form.addRow("Stationary power", self.standby_power_edit)
        energy_form.addRow("Regenerative efficiency", self.regen_efficiency_edit)
        car_layout.addWidget(energy_box)
        car_layout.addStretch(1)

        reliability_page = QWidget()
        tabs.addTab(reliability_page, "Health and reliability")
        reliability_layout = QVBoxLayout(reliability_page)
        health_box = QGroupBox("Condition")
        health_form = QFormLayout(health_box)
        self.health_edit = _double_input(
            self.lift.get("health_percent", 100.0), minimum=0.0, maximum=100.0,
            decimals=1, suffix=" %", step=1.0,
            tooltip="Initial lift health at the start of the run.",
        )
        self.health_loss_edit = _double_input(
            self.lift.get("health_loss_per_journey_percent", 0.05),
            minimum=0.0, maximum=100.0, decimals=4, suffix=" % / journey", step=0.01,
        )
        self.minimum_health_edit = _double_input(
            self.lift.get("minimum_operational_health_percent", 20.0),
            minimum=0.0, maximum=100.0, decimals=1, suffix=" %", step=1.0,
            tooltip="Below this health the lift is treated as unavailable.",
        )
        self.health_speed_penalty_edit = _double_input(
            self.lift.get("health_speed_penalty_at_zero", 0.5),
            minimum=0.01, maximum=1.0, decimals=3, step=0.05,
            tooltip="Travel-speed multiplier at 0% health. 0.5 means half speed.",
        )
        health_form.addRow("Initial health", self.health_edit)
        health_form.addRow("Health lost per journey", self.health_loss_edit)
        health_form.addRow("Unavailable below", self.minimum_health_edit)
        health_form.addRow("Speed factor at 0% health", self.health_speed_penalty_edit)
        reliability_layout.addWidget(health_box)

        failure_box = QGroupBox("Failure and repair")
        failure_form = QFormLayout(failure_box)
        self.mtbf_edit = _double_input(
            self.lift.get("mean_time_between_failures_hours", 720.0),
            minimum=0.01, maximum=10_000_000.0, decimals=2, suffix=" h", step=1.0,
            tooltip="Nominal mean time between failures at full health.",
        )
        self.mttr_edit = _double_input(
            self.lift.get("mean_time_to_repair_hours", 4.0),
            minimum=0.0, maximum=100_000.0, decimals=2, suffix=" h", step=0.5,
        )
        failure_form.addRow("Mean time between failures", self.mtbf_edit)
        failure_form.addRow("Mean repair duration", self.mttr_edit)
        reliability_layout.addWidget(failure_box)
        reliability_layout.addWidget(
            _dialog_intro(
                "Scenario outages override health. Outside a scenario, lower health slows the lift, "
                "reduces its effective failure interval and eventually removes it from service."
            )
        )
        reliability_layout.addStretch(1)

        self.floors_edit.textChanged.connect(self._refresh_positions_preview)
        self._refresh_positions_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def _floor_height_m(self):
        parent = self.parent()
        store = getattr(parent, "store", None)
        data = getattr(store, "data", {}) if store is not None else {}
        return float((data.get("building", {}) or {}).get("floor_height_m", 4.0) or 4.0)

    def _default_speed_m_per_sec(self):
        if self.lift.get("speed_m_per_sec") is not None:
            return float(self.lift.get("speed_m_per_sec") or 0.0)
        return float(self.lift.get("speed_floors_per_sec", 0.45) or 0.45) * self._floor_height_m()

    def _normalise_floor_locations(self, floor_locations):
        normalised = {}
        for key, value in (floor_locations or {}).items():
            try:
                floor = int(key)
                if isinstance(value, dict):
                    x = float(value.get("x", self.default_x))
                    y = float(value.get("y", self.default_y))
                else:
                    x = float(value[0])
                    y = float(value[1])
                normalised[floor] = (x, y)
            except Exception:
                continue
        return normalised

    def _parse_floors(self):
        try:
            floors = [
                int(x.strip()) for x in self.floors_edit.text().split(",") if x.strip()
            ]
        except ValueError as exc:
            raise ValueError("Floors served must be whole numbers separated by commas, for example 0, 1, 2.") from exc
        if not floors:
            raise ValueError("Enter at least one floor served by this lift.")
        return sorted(set(floors))

    def _build_floor_locations(self, floors):
        positions = {}
        for floor in floors:
            x, y = self.existing_floor_locations.get(
                floor,
                (self.default_x, self.default_y),
            )
            positions[floor] = (float(x), float(y))
        return positions

    def _refresh_positions_preview(self):
        try:
            floors = self._parse_floors()
            positions = self._build_floor_locations(floors)
            lines = [
                f"Floor {floor}: X {pos[0]:.3f} m, Y {pos[1]:.3f} m"
                for floor, pos in positions.items()
            ]
            self.positions_edit.setPlainText("\n".join(lines))
            if int(self.start_floor_edit.value()) not in floors:
                self.start_floor_edit.setValue(floors[0])
        except Exception as exc:
            self.positions_edit.setPlainText(f"Check floors served: {exc}")

    def _suggest_next_lift_id(self):
        existing_ids = set()
        parent = self.parent()
        if parent and hasattr(parent, "store"):
            for lift in parent.store.data.get("lifts", []):
                lift_id = str(lift.get("id", "")).strip()
                if lift_id:
                    existing_ids.add(lift_id)
        nums = []
        for lift_id in existing_ids:
            upper = lift_id.upper()
            if upper.startswith("LIFT-"):
                tail = lift_id[5:]
            elif upper.startswith("LIFT"):
                tail = lift_id[4:]
            else:
                continue
            tail = tail.strip()
            if tail.isdigit():
                nums.append(int(tail))
        return f"Lift-{max(nums, default=0) + 1}"

    def accept(self):
        try:
            lift_id = self.id_edit.text().strip()
            if not lift_id:
                raise ValueError("Enter a lift name.")
            floors = self._parse_floors()
            start_floor = int(self.start_floor_edit.value())
            if start_floor not in floors:
                raise ValueError("The starting floor must be included in the floors served.")
            positions = self._build_floor_locations(floors)
            self.result = {
                "id": lift_id,
                "served_floors": floors,
                "speed_m_per_sec": float(self.speed_edit.value()),
                "door_time_sec": float(self.door_edit.value()),
                "boarding_time_sec": float(self.board_edit.value()),
                "capacity_length_m": float(self.capacity_length_edit.value()),
                "capacity_width_m": float(self.capacity_width_edit.value()),
                "capacity_height_m": float(self.capacity_height_edit.value()),
                "car_mass_kg": float(self.car_mass_edit.value()),
                "counterweight_ratio": float(self.counterweight_edit.value()),
                "travel_efficiency": float(self.travel_efficiency_edit.value()),
                "door_power_w": float(self.door_power_edit.value()),
                "standby_power_w": float(self.standby_power_edit.value()),
                "regen_efficiency": float(self.regen_efficiency_edit.value()),
                "health_percent": float(self.health_edit.value()),
                "health_loss_per_journey_percent": float(self.health_loss_edit.value()),
                "mean_time_between_failures_hours": float(self.mtbf_edit.value()),
                "mean_time_to_repair_hours": float(self.mttr_edit.value()),
                "minimum_operational_health_percent": float(self.minimum_health_edit.value()),
                "health_speed_penalty_at_zero": float(self.health_speed_penalty_edit.value()),
                "start_floor": start_floor,
                "floor_locations": positions,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check lift details", str(exc))


class LiftListDialog(QDialog):
    def __init__(self, parent, store, on_changed):
        super().__init__(parent)
        self.setWindowTitle("Lifts")
        self.resize(980, 460)
        self.store = store
        self.on_changed = on_changed

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Floors",
                "Speed m/sec",
                "Door s",
                "Board s",
                "Capacity LxWxH",
                "Stationary W",
                "Start floor",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        for text, slot in [
            ("Add", self.add_lift),
            ("Edit", self.edit_lift),
            ("Delete", self.delete_lift),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.refresh()
        _polish_dialog(self)

    def refresh(self):
        self.table.setRowCount(0)
        for lift in self.store.data.get("lifts", []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            floors = ", ".join(str(x) for x in lift.get("served_floors", []))
            dims = " x ".join(
                str(lift.get(key, ""))
                for key in (
                    "capacity_length_m",
                    "capacity_width_m",
                    "capacity_height_m",
                )
            )
            values = [
                lift.get("id", ""),
                floors,
                self._lift_speed_m_per_sec(lift),
                lift.get("door_time_sec", ""),
                lift.get("boarding_time_sec", ""),
                dims,
                lift.get("standby_power_w", ""),
                lift.get("start_floor", ""),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def _lift_speed_m_per_sec(self, lift):
        speed_m_per_sec = lift.get("speed_m_per_sec")
        if speed_m_per_sec not in (None, ""):
            return speed_m_per_sec
        floor_height_m = float(
            (self.store.data.get("building", {}) or {}).get("floor_height_m", 4.0)
            or 4.0
        )
        return float(lift.get("speed_floors_per_sec", 0.45) or 0.45) * floor_height_m

    def selected_lift(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        lifts = self.store.data.get("lifts", [])
        return lifts[idx] if 0 <= idx < len(lifts) else None

    def default_floor(self):
        floor_spin = getattr(self.parent(), "floor_spin", None)
        return int(floor_spin.value()) if floor_spin is not None else 0

    def save_result(self, result, old_id=None):
        if old_id and old_id != result["id"]:
            self.store.delete_lift(old_id)
        self.store.upsert_lift(
            result["id"],
            result["served_floors"],
            result["floor_locations"],
            result["speed_m_per_sec"],
            result["door_time_sec"],
            result["boarding_time_sec"],
            result["capacity_length_m"],
            result["capacity_width_m"],
            result["capacity_height_m"],
            result["car_mass_kg"],
            result["counterweight_ratio"],
            result["travel_efficiency"],
            result["door_power_w"],
            result["standby_power_w"],
            result["regen_efficiency"],
            result["health_percent"],
            result["health_loss_per_journey_percent"],
            result["mean_time_between_failures_hours"],
            result["mean_time_to_repair_hours"],
            result["minimum_operational_health_percent"],
            result["health_speed_penalty_at_zero"],
            result["start_floor"],
        )
        self.on_changed()
        self.refresh()

    def add_lift(self):
        dialog = LiftEditorDialog(self.parent(), default_floor=self.default_floor())
        if dialog.exec() and dialog.result:
            self.save_result(dialog.result)

    def edit_lift(self):
        lift = self.selected_lift()
        if lift is None:
            return
        dialog = LiftEditorDialog(self.parent(), lift)
        if dialog.exec() and dialog.result:
            self.save_result(dialog.result, old_id=lift.get("id"))

    def delete_lift(self):
        lift = self.selected_lift()
        if lift is None:
            return
        lift_id = str(lift.get("id", "")).strip()
        if not lift_id:
            return
        if QMessageBox.question(self, "Delete lift", f"Delete {lift_id}?") != QMessageBox.Yes:
            return
        self.store.delete_lift(lift_id)
        self.on_changed()
        self.refresh()


def _normalise_amr_payload_slots(amr):
    """Return the AMR payload slot list, migrating legacy single-slot fields."""
    slots = amr.get("payload_slots", []) if isinstance(amr, dict) else []
    clean = []

    if isinstance(slots, list):
        for idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                continue
            clean.append(
                {
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
            )

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


def _amr_payload_slot_summary(amr):
    slots = _normalise_amr_payload_slots(amr)
    if len(slots) == 1:
        slot = slots[0]
        return (
            f"1 slot ({slot.get('payload_capacity_kg', 0):g} kg, "
            f"{slot.get('payload_length_capacity_m', 0):g} x "
            f"{slot.get('payload_width_capacity_m', 0):g} x "
            f"{slot.get('payload_height_capacity_m', 0):g} m)"
        )
    return f"{len(slots)} slots"


class AMRPayloadSlotDialog(QDialog):
    """Guided editor for one AMR payload position."""

    def __init__(self, parent, seed=None, default_name="Slot 1"):
        super().__init__(parent)
        seed = dict(seed or {})
        self.setWindowTitle("Payload slot")
        self.setMinimumWidth(520)
        self.result = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "A payload slot is one physical carrying position on the AMR. Enter the clear carrying "
                "envelope and select the orientations the mechanism can safely support."
            )
        )
        box = QGroupBox("Slot capacity")
        form = QFormLayout(box)
        self.name_edit = QLineEdit(str(seed.get("name", default_name)))
        self.name_edit.setPlaceholderText("For example, Front slot")
        self.weight_spin = _double_input(
            seed.get("payload_capacity_kg", 100.0), minimum=0.001,
            maximum=1_000_000.0, decimals=2, suffix=" kg", step=5.0,
        )
        self.length_spin = _double_input(
            seed.get("payload_length_capacity_m", 1.0), minimum=0.001,
            maximum=100.0, decimals=3, suffix=" m", step=0.05,
            tooltip="Maximum payload dimension along the AMR's direction of travel when carried lengthways.",
        )
        self.width_spin = _double_input(
            seed.get("payload_width_capacity_m", 1.0), minimum=0.001,
            maximum=100.0, decimals=3, suffix=" m", step=0.05,
        )
        self.height_spin = _double_input(
            seed.get("payload_height_capacity_m", 1.0), minimum=0.001,
            maximum=100.0, decimals=3, suffix=" m", step=0.05,
        )
        allowed = {
            str(value).strip().lower()
            for value in (seed.get("allowed_payload_orientations") or ["lengthways", "sideways"])
        }
        orientation_widget = QWidget()
        orientation_layout = QHBoxLayout(orientation_widget)
        orientation_layout.setContentsMargins(0, 0, 0, 0)
        self.lengthways_check = QCheckBox("Lengthways")
        self.sideways_check = QCheckBox("Sideways")
        self.lengthways_check.setChecked("lengthways" in allowed)
        self.sideways_check.setChecked("sideways" in allowed)
        orientation_layout.addWidget(self.lengthways_check)
        orientation_layout.addWidget(self.sideways_check)
        orientation_layout.addStretch(1)
        form.addRow("Slot name", self.name_edit)
        form.addRow("Maximum payload weight", self.weight_spin)
        form.addRow("Clear carrying length", self.length_spin)
        form.addRow("Clear carrying width", self.width_spin)
        form.addRow("Clear carrying height", self.height_spin)
        form.addRow("Supported orientations", orientation_widget)
        layout.addWidget(box)
        layout.addWidget(
            _dialog_intro(
                "Sideways carriage swaps the payload length and width when checking AMR capacity, lift fit "
                "and whether a corridor must operate as a single lane."
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Check payload slot", "Enter a name for this payload slot.")
            return
        orientations = []
        if self.lengthways_check.isChecked():
            orientations.append("lengthways")
        if self.sideways_check.isChecked():
            orientations.append("sideways")
        if not orientations:
            QMessageBox.warning(
                self,
                "Check payload slot",
                "Select at least one supported carrying orientation.",
            )
            return
        self.result = {
            "name": name,
            "payload_capacity_kg": float(self.weight_spin.value()),
            "payload_length_capacity_m": float(self.length_spin.value()),
            "payload_width_capacity_m": float(self.width_spin.value()),
            "payload_height_capacity_m": float(self.height_spin.value()),
            "allowed_payload_orientations": orientations,
        }
        super().accept()


class AMREditorDialog(QDialog):
    def __init__(self, parent, location_names, seed=None, default_amr_id="AMR-1"):
        super().__init__(parent)
        self.seed = dict(seed or {})
        self.setWindowTitle("Edit AMR type" if self.seed else "Add AMR type")
        self.resize(820, 690)
        self.setMinimumSize(700, 560)
        self.result = None
        self.original_department_id = str(self.seed.get("id", "") or "").strip()
        self.original_department_name = str(self.seed.get("name", "") or "").strip()
        self.location_names = sorted(location_names)
        self.payload_slots = _normalise_amr_payload_slots(self.seed)

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Define an AMR type and how many identical units are available. Physical dimensions affect "
                "corridor lanes, doors and lifts; payload slots determine which loads can be carried."
            )
        )
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        general_page = QWidget()
        tabs.addTab(general_page, "AMR details")
        general_layout = QVBoxLayout(general_page)
        identity_box = QGroupBox("Identity and fleet quantity")
        identity_form = QFormLayout(identity_box)
        self.id_edit = QLineEdit(str(self.seed.get("id", default_amr_id)))
        self.id_edit.setPlaceholderText("For example, AMR-A")
        self.quantity_edit = _integer_input(
            self.seed.get("quantity", 1), minimum=1, maximum=100_000,
            suffix=" unit(s)", tooltip="Number of identical physical AMRs of this type.",
        )
        identity_form.addRow("AMR type name", self.id_edit)
        identity_form.addRow("Fleet quantity", self.quantity_edit)
        general_layout.addWidget(identity_box)

        physical_box = QGroupBox("Physical envelope and movement")
        physical_form = QFormLayout(physical_box)
        self.amr_length_edit = _double_input(
            self.seed.get("length_m", 0.8), minimum=0.05, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.amr_width_edit = _double_input(
            self.seed.get("width_m", 0.6), minimum=0.05, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.amr_height_edit = _double_input(
            self.seed.get("height_m", 1.2), minimum=0.05, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.speed_edit = _double_input(
            self.seed.get("speed_m_per_sec", 1.0), minimum=0.01, maximum=20.0,
            decimals=3, suffix=" m/s", step=0.05,
            tooltip="Unrestricted travel speed before people, scenarios and corridor restrictions are applied.",
        )
        self.motor_power_edit = _double_input(
            self.seed.get("motor_power_w", 250), minimum=0.0, maximum=10_000_000.0,
            decimals=1, suffix=" W", step=10.0,
        )
        physical_form.addRow("Overall length", self.amr_length_edit)
        physical_form.addRow("Overall width", self.amr_width_edit)
        physical_form.addRow("Overall height", self.amr_height_edit)
        physical_form.addRow("Normal travel speed", self.speed_edit)
        physical_form.addRow("Travel motor power", self.motor_power_edit)
        general_layout.addWidget(physical_box)
        general_layout.addStretch(1)

        battery_page = QWidget()
        tabs.addTab(battery_page, "Battery and charging")
        battery_layout = QVBoxLayout(battery_page)
        battery_box = QGroupBox("Battery model")
        battery_form = QFormLayout(battery_box)
        self.battery_capacity_edit = _double_input(
            self.seed.get("battery_capacity_kwh", 1.0), minimum=0.001,
            maximum=1_000_000.0, decimals=3, suffix=" kWh", step=0.1,
        )
        self.charge_rate_edit = _double_input(
            self.seed.get("battery_charge_rate_kw", 0.5), minimum=0.001,
            maximum=1_000_000.0, decimals=3, suffix=" kW", step=0.1,
        )
        self.recharge_threshold_edit = _double_input(
            self.seed.get("recharge_threshold_percent", 20), minimum=0.0,
            maximum=100.0, decimals=1, suffix=" %", step=1.0,
            tooltip="The AMR requests charging when state of charge reaches this level.",
        )
        self.battery_soc_edit = _double_input(
            self.seed.get("battery_soc_percent", 100), minimum=0.0,
            maximum=100.0, decimals=1, suffix=" %", step=1.0,
            tooltip="Initial battery state of charge at the start of the simulation.",
        )
        self.charge_time_label = QLabel()
        self.charge_time_label.setWordWrap(True)
        battery_form.addRow("Usable battery capacity", self.battery_capacity_edit)
        battery_form.addRow("Charging power", self.charge_rate_edit)
        battery_form.addRow("Request charging at", self.recharge_threshold_edit)
        battery_form.addRow("Initial state of charge", self.battery_soc_edit)
        battery_form.addRow("Indicative full-charge time", self.charge_time_label)
        battery_layout.addWidget(battery_box)
        battery_layout.addWidget(
            _dialog_intro(
                "Charging can occur only in AMR parking spaces marked as charger-equipped. The charger estimate "
                "uses overlapping charging periods, not the total number of parking spaces."
            )
        )
        battery_layout.addStretch(1)
        self.battery_capacity_edit.valueChanged.connect(self._update_charge_summary)
        self.charge_rate_edit.valueChanged.connect(self._update_charge_summary)
        self._update_charge_summary()

        slots_page = QWidget()
        tabs.addTab(slots_page, "Payload slots")
        slots_layout = QVBoxLayout(slots_page)
        slots_layout.addWidget(
            _dialog_intro(
                "Each row is a physical carrying position. Double-click a row to edit it. Multi-stop route "
                "batching is available only when the AMR has more than one slot."
            )
        )
        self.slots_table = QTableWidget(0, 6)
        self.slots_table.setHorizontalHeaderLabels(
            ["Slot", "Weight", "Length", "Width", "Height", "Orientations"]
        )
        _configure_data_table(self.slots_table)
        self.slots_table.doubleClicked.connect(self.edit_payload_slot)
        slots_layout.addWidget(self.slots_table, 1)
        slot_buttons = QHBoxLayout()
        slots_layout.addLayout(slot_buttons)
        for text, slot in [
            ("Add slot", self.add_payload_slot),
            ("Edit selected", self.edit_payload_slot),
            ("Duplicate selected", self.duplicate_payload_slot),
            ("Delete selected", self.delete_payload_slot),
        ]:
            button = QPushButton(text)
            button.clicked.connect(slot)
            slot_buttons.addWidget(button)
        slot_buttons.addStretch(1)
        self.multi_stop_check = QCheckBox(
            "Allow tasks to be batched into one multi-stop route when several slots are available"
        )
        self.multi_stop_check.setChecked(
            bool(self.seed.get("multi_stop_enabled", len(self.payload_slots) > 1))
        )
        slots_layout.addWidget(self.multi_stop_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_slots_table()
        _polish_dialog(self)

    def _update_charge_summary(self):
        rate = float(self.charge_rate_edit.value())
        capacity = float(self.battery_capacity_edit.value())
        if rate <= 0.0:
            self.charge_time_label.setText("Charging power must be greater than zero.")
            return
        hours = capacity / rate
        self.charge_time_label.setText(
            f"Approximately {hours:.2f} hours from empty to full before charging losses."
        )

    def _refresh_slots_table(self):
        self.slots_table.setRowCount(0)
        for slot in self.payload_slots:
            row = self.slots_table.rowCount()
            self.slots_table.insertRow(row)
            values = [
                slot.get("name", f"Slot {row + 1}"),
                f"{float(slot.get('payload_capacity_kg', 0.0)):g} kg",
                f"{float(slot.get('payload_length_capacity_m', 0.0)):g} m",
                f"{float(slot.get('payload_width_capacity_m', 0.0)):g} m",
                f"{float(slot.get('payload_height_capacity_m', 0.0)):g} m",
                ", ".join(
                    str(value).replace("ways", "ways").title()
                    for value in slot.get(
                        "allowed_payload_orientations", ["lengthways", "sideways"]
                    )
                ),
            ]
            for col, value in enumerate(values):
                self.slots_table.setItem(row, col, QTableWidgetItem(str(value)))
        self.multi_stop_check.setEnabled(len(self.payload_slots) > 1)
        if len(self.payload_slots) <= 1:
            self.multi_stop_check.setChecked(False)

    def _selected_slot_row(self):
        row = self.slots_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Payload slots", "Select a payload slot first.")
            return None
        return row

    def add_payload_slot(self):
        dialog = AMRPayloadSlotDialog(
            self,
            default_name=f"Slot {len(self.payload_slots) + 1}",
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.payload_slots.append(dialog.result)
            self._refresh_slots_table()
            self.slots_table.selectRow(len(self.payload_slots) - 1)

    def edit_payload_slot(self):
        row = self._selected_slot_row()
        if row is None:
            return
        dialog = AMRPayloadSlotDialog(self, self.payload_slots[row])
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.payload_slots[row] = dialog.result
            self._refresh_slots_table()
            self.slots_table.selectRow(row)

    def duplicate_payload_slot(self):
        row = self._selected_slot_row()
        if row is None:
            return
        duplicate = dict(self.payload_slots[row])
        duplicate["allowed_payload_orientations"] = list(
            duplicate.get("allowed_payload_orientations", [])
        )
        duplicate["name"] = f"{duplicate.get('name', f'Slot {row + 1}')} copy"
        self.payload_slots.insert(row + 1, duplicate)
        self._refresh_slots_table()
        self.slots_table.selectRow(row + 1)

    def delete_payload_slot(self):
        row = self._selected_slot_row()
        if row is None:
            return
        if len(self.payload_slots) <= 1:
            QMessageBox.warning(
                self, "Payload slots", "An AMR type must have at least one payload slot."
            )
            return
        if QMessageBox.question(
            self,
            "Delete payload slot",
            f"Delete '{self.payload_slots[row].get('name', f'Slot {row + 1}')}'?",
        ) != QMessageBox.Yes:
            return
        del self.payload_slots[row]
        self._refresh_slots_table()
        if self.payload_slots:
            self.slots_table.selectRow(min(row, len(self.payload_slots) - 1))

    def _collect_payload_slots(self):
        slots = []
        for index, raw in enumerate(self.payload_slots, start=1):
            slot = dict(raw)
            slot["name"] = str(slot.get("name", "")).strip() or f"Slot {index}"
            slot["payload_capacity_kg"] = float(slot.get("payload_capacity_kg", 0.0) or 0.0)
            slot["payload_length_capacity_m"] = float(
                slot.get("payload_length_capacity_m", 0.0) or 0.0
            )
            slot["payload_width_capacity_m"] = float(
                slot.get("payload_width_capacity_m", 0.0) or 0.0
            )
            slot["payload_height_capacity_m"] = float(
                slot.get("payload_height_capacity_m", 0.0) or 0.0
            )
            slot["allowed_payload_orientations"] = [
                str(value).strip().lower()
                for value in slot.get("allowed_payload_orientations", [])
                if str(value).strip().lower() in {"lengthways", "sideways"}
            ]
            if slot["payload_capacity_kg"] <= 0.0:
                raise ValueError(f"{slot['name']} must have a positive weight capacity.")
            if min(
                slot["payload_length_capacity_m"],
                slot["payload_width_capacity_m"],
                slot["payload_height_capacity_m"],
            ) <= 0.0:
                raise ValueError(f"{slot['name']} must have positive carrying dimensions.")
            if not slot["allowed_payload_orientations"]:
                raise ValueError(f"{slot['name']} must support at least one carrying orientation.")
            slots.append(slot)
        if not slots:
            raise ValueError("Add at least one payload slot.")
        return slots

    def accept(self):
        try:
            amr_id = self.id_edit.text().strip()
            if not amr_id:
                raise ValueError("Enter an AMR type name.")
            payload_slots = self._collect_payload_slots()
            primary_slot = payload_slots[0]
            multi_stop_enabled = bool(
                self.multi_stop_check.isChecked() and len(payload_slots) > 1
            )
            self.result = {
                "id": amr_id,
                "quantity": int(self.quantity_edit.value()),
                "payload_slots": payload_slots,
                "payload_capacity_kg": float(primary_slot["payload_capacity_kg"]),
                "payload_length_capacity_m": float(primary_slot["payload_length_capacity_m"]),
                "payload_width_capacity_m": float(primary_slot["payload_width_capacity_m"]),
                "payload_height_capacity_m": float(primary_slot["payload_height_capacity_m"]),
                "multi_stop_enabled": multi_stop_enabled,
                "manual_task_compatible": len(payload_slots) == 1,
                "length_m": float(self.amr_length_edit.value()),
                "width_m": float(self.amr_width_edit.value()),
                "height_m": float(self.amr_height_edit.value()),
                "speed_m_per_sec": float(self.speed_edit.value()),
                "motor_power_w": float(self.motor_power_edit.value()),
                "battery_capacity_kwh": float(self.battery_capacity_edit.value()),
                "battery_charge_rate_kw": float(self.charge_rate_edit.value()),
                "recharge_threshold_percent": float(self.recharge_threshold_edit.value()),
                "battery_soc_percent": float(self.battery_soc_edit.value()),
            }
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check AMR details", str(exc))


class StaffDeliveryResourceEditorDialog(QDialog):
    def __init__(self, parent, location_names, seed=None, default_id="PORTER-1"):
        super().__init__(parent)
        self.seed = normalise_staff_delivery_resource(seed or {"id": default_id})
        self.result = None
        self.setWindowTitle("Edit staff delivery type" if seed else "Add staff delivery type")
        self.resize(720, 650)

        layout = QVBoxLayout(self)
        layout.addWidget(_dialog_intro(
            "Define staff who transport payloads through the building. This is separate from "
            "staff-assisted handling at pickup or delivery locations."
        ))
        form = QFormLayout()
        layout.addLayout(form)

        self.id_edit = QLineEdit(self.seed.get("id", default_id))
        self.quantity_edit = _integer_input(
            self.seed.get("quantity", 1), minimum=1, maximum=100_000, suffix=" person(s)"
        )
        self.base_combo = QComboBox()
        self.base_combo.addItems([""] + sorted(location_names))
        self.base_combo.setCurrentText(self.seed.get("base_location", ""))
        self.speed_edit = _double_input(
            self.seed.get("speed_m_per_sec", 1.2), minimum=0.01, maximum=10.0,
            decimals=3, suffix=" m/s", step=0.05,
        )
        self.weight_edit = _double_input(
            self.seed.get("payload_capacity_kg", 25.0), minimum=0.01, maximum=10_000.0,
            decimals=2, suffix=" kg", step=1.0,
        )
        self.length_edit = _double_input(
            self.seed.get("payload_length_capacity_m", 1.0), minimum=0.01, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.width_edit = _double_input(
            self.seed.get("payload_width_capacity_m", 0.8), minimum=0.01, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.height_edit = _double_input(
            self.seed.get("payload_height_capacity_m", 1.5), minimum=0.01, maximum=20.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.turnaround_edit = _double_input(
            self.seed.get("turnaround_time_sec", 300.0), minimum=0.0, maximum=86_400.0,
            decimals=0, suffix=" s", step=30.0,
        )

        self.shift_start_edit = QTimeEdit()
        self.shift_start_edit.setDisplayFormat("HH:mm")
        self.shift_start_edit.setTime(
            QTime.fromString(self.seed.get("shift_start_time", "07:00"), "HH:mm")
        )
        self.shift_end_edit = QTimeEdit()
        self.shift_end_edit.setDisplayFormat("HH:mm")
        self.shift_end_edit.setTime(
            QTime.fromString(self.seed.get("shift_end_time", "15:00"), "HH:mm")
        )
        self.days_selector = DayOfWeekSelector(selected=self.seed.get("days_active", []))
        self.breaks_edit = QPlainTextEdit()
        self.breaks_edit.setPlaceholderText("Meal break, 12:00, 12:30")
        self.breaks_edit.setPlainText("\n".join(
            f"{item.get('name', 'Break')}, {item.get('start_time', '')}, {item.get('end_time', '')}"
            for item in self.seed.get("breaks", [])
        ))
        self.breaks_edit.setMaximumHeight(90)
        self.capabilities_edit = QLineEdit(", ".join(self.seed.get("capabilities", [])))
        self.capabilities_edit.setPlaceholderText("case_cart, secure_load")

        form.addRow("Staff type name", self.id_edit)
        form.addRow("Quantity", self.quantity_edit)
        form.addRow("Base location", self.base_combo)
        form.addRow("Walking speed", self.speed_edit)
        form.addRow("Maximum payload weight", self.weight_edit)
        form.addRow("Payload length allowance", self.length_edit)
        form.addRow("Payload width allowance", self.width_edit)
        form.addRow("Payload height allowance", self.height_edit)
        form.addRow("Turnaround after delivery", self.turnaround_edit)
        form.addRow("Shift starts", self.shift_start_edit)
        form.addRow("Shift ends", self.shift_end_edit)
        form.addRow("Working days", self.days_selector)
        form.addRow("Breaks (name, start, end)", self.breaks_edit)
        form.addRow("Capabilities", self.capabilities_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def _breaks(self):
        result = []
        for line_number, line in enumerate(self.breaks_edit.toPlainText().splitlines(), start=1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 3:
                raise ValueError(f"Break line {line_number} must contain name, start and end.")
            name, start, end = parts
            if not QTime.fromString(start, "HH:mm").isValid() or not QTime.fromString(end, "HH:mm").isValid():
                raise ValueError(f"Break line {line_number} must use HH:mm times.")
            result.append({"name": name or "Break", "start_time": start, "end_time": end})
        return result

    def accept(self):
        try:
            resource_id = self.id_edit.text().strip()
            if not resource_id:
                raise ValueError("Enter a staff delivery type name.")
            if not self.base_combo.currentText().strip():
                raise ValueError("Select a base location.")
            days = self.days_selector.selected_days()
            if not days:
                raise ValueError("Select at least one working day.")
            self.result = normalise_staff_delivery_resource({
                "id": resource_id,
                "quantity": int(self.quantity_edit.value()),
                "base_location": self.base_combo.currentText().strip(),
                "speed_m_per_sec": float(self.speed_edit.value()),
                "payload_capacity_kg": float(self.weight_edit.value()),
                "payload_length_capacity_m": float(self.length_edit.value()),
                "payload_width_capacity_m": float(self.width_edit.value()),
                "payload_height_capacity_m": float(self.height_edit.value()),
                "turnaround_time_sec": float(self.turnaround_edit.value()),
                "shift_start_time": self.shift_start_edit.time().toString("HH:mm"),
                "shift_end_time": self.shift_end_edit.time().toString("HH:mm"),
                "days_active": days,
                "breaks": self._breaks(),
                "capabilities": [x.strip() for x in self.capabilities_edit.text().split(",") if x.strip()],
            })
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check staff delivery resource", str(exc))


class PayloadTrackedItemDialog(QDialog):
    MODES = [
        ("Scheduled demand", "scheduled"),
        ("Replenish at a threshold", "threshold"),
        ("Continuous consumption", "continuous"),
        ("Sporadic demand", "sporadic"),
        ("Hybrid demand", "hybrid"),
        ("Scheduled with threshold replenishment", "scheduled_threshold"),
        ("Scheduled with sporadic variation", "scheduled_sporadic"),
    ]

    def __init__(self, parent, seed=None, payload_names=None, location_names=None):
        super().__init__(parent)
        self.seed = dict(seed or {})
        self.setWindowTitle("Tracked item")
        self.setMinimumWidth(620)
        self.result = None
        self.payload_names = sorted(payload_names or [])
        self.location_names = sorted(location_names or [])
        self.selected_source_locations = (
            [str(self.seed.get("source_location", "")).strip()]
            if str(self.seed.get("source_location", "")).strip()
            else []
        )

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Tracked items model stock held inside a payload, such as linen packs, consumables or meal trays. "
                "The threshold controls when replenishment is requested."
            )
        )
        stock_box = QGroupBox("Stock settings")
        form = QFormLayout(stock_box)
        self.name_edit = QLineEdit(str(self.seed.get("name", "")))
        self.name_edit.setPlaceholderText("For example, Clean linen packs")
        self.max_edit = _double_input(
            self.seed.get("max", 100), minimum=0.001, maximum=1_000_000_000.0,
            decimals=2, suffix=" units", step=1.0,
        )
        self.threshold_edit = _double_input(
            self.seed.get("top_up_threshold", 15), minimum=0.0,
            maximum=1_000_000_000.0, decimals=2, suffix=" units", step=1.0,
            tooltip="Replenishment is requested when stock reaches this level.",
        )
        self.usage_rate_combo = QComboBox()
        for label, value in self.MODES:
            self.usage_rate_combo.addItem(label, value)
        mode = str(self.seed.get("usage_rate", "scheduled_sporadic") or "scheduled_sporadic")
        self.usage_rate_combo.setCurrentIndex(max(0, self.usage_rate_combo.findData(mode)))
        self.consumption_edit = _double_input(
            self.seed.get("consumption_per_day", 0.0), minimum=0.0,
            maximum=1_000_000_000.0, decimals=3, suffix=" units/day", step=1.0,
        )
        form.addRow("Item name", self.name_edit)
        form.addRow("Maximum stock", self.max_edit)
        form.addRow("Top-up threshold", self.threshold_edit)
        form.addRow("Demand pattern", self.usage_rate_combo)
        form.addRow("Average daily consumption", self.consumption_edit)
        layout.addWidget(stock_box)

        supply_box = QGroupBox("Replenishment source")
        supply_form = QFormLayout(supply_box)
        self.exchange_payload_combo = QComboBox()
        self.exchange_payload_combo.addItem("No exchange payload", "")
        for name in self.payload_names:
            self.exchange_payload_combo.addItem(name, name)
        exchange = str(self.seed.get("exchange_payload", "") or "")
        self.exchange_payload_combo.setCurrentIndex(
            max(0, self.exchange_payload_combo.findData(exchange))
        )
        source_widget = QWidget()
        source_row = QHBoxLayout(source_widget)
        source_row.setContentsMargins(0, 0, 0, 0)
        self.source_location_summary = QLabel("No source selected")
        self.source_location_summary.setWordWrap(True)
        source_btn = QPushButton("Choose source…")
        source_btn.clicked.connect(self.pick_source_locations)
        clear_source_btn = QPushButton("Clear")
        clear_source_btn.clicked.connect(self.clear_source_locations)
        source_row.addWidget(self.source_location_summary, 1)
        source_row.addWidget(source_btn)
        source_row.addWidget(clear_source_btn)
        supply_form.addRow("Exchange payload", self.exchange_payload_combo)
        supply_form.addRow("Source location", source_widget)
        layout.addWidget(supply_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_source_location_summary()
        _polish_dialog(self)

    def pick_source_locations(self):
        picker = MultiSelectPicker(
            self,
            "Select a replenishment source",
            self.location_names,
            selected=self.selected_source_locations,
            group_resolver=lambda _item: "Locations",
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_source_locations = sorted(picker.result[:1])
            self.refresh_source_location_summary()

    def clear_source_locations(self):
        self.selected_source_locations = []
        self.refresh_source_location_summary()

    def refresh_source_location_summary(self):
        if not self.selected_source_locations:
            self.source_location_summary.setText("No source selected")
        else:
            self.source_location_summary.setText(self.selected_source_locations[0])

    def accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Check tracked item", "Enter an item name.")
            return
        max_qty = float(self.max_edit.value())
        threshold = float(self.threshold_edit.value())
        if threshold > max_qty:
            QMessageBox.warning(
                self,
                "Check tracked item",
                "The top-up threshold cannot be greater than the maximum stock.",
            )
            return
        self.result = {
            "name": name,
            "max": max_qty,
            "top_up_threshold": threshold,
            "usage_rate": str(self.usage_rate_combo.currentData() or "scheduled_sporadic"),
            "consumption_per_day": float(self.consumption_edit.value()),
            "exchange_payload": str(self.exchange_payload_combo.currentData() or "").strip(),
            "source_location": (
                self.selected_source_locations[0]
                if self.selected_source_locations
                else ""
            ),
        }
        super().accept()


class PayloadEditorDialog(QDialog):
    def __init__(self, parent, seed=None, payload_names=None, location_names=None):
        super().__init__(parent)
        self.seed = dict(seed or {})
        self.setWindowTitle("Edit payload type" if self.seed.get("name") else "Add payload type")
        self.resize(820, 650)
        self.setMinimumSize(700, 540)
        self.result = None
        self.tracked_items = self._normalise_tracked_items(self.seed.get("items", []))
        self.payload_names = sorted(payload_names or [])
        self.location_names = sorted(location_names or [])

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Define the physical load carried by an AMR. Dimensions and carrying orientation are checked "
                "against AMR slots, lift cars, doors and corridor lane widths."
            )
        )
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        physical_page = QWidget()
        tabs.addTab(physical_page, "Payload details")
        physical_layout = QVBoxLayout(physical_page)
        identity_box = QGroupBox("Identity and physical envelope")
        identity_form = QFormLayout(identity_box)
        self.name_edit = QLineEdit(str(self.seed.get("name", "")))
        self.name_edit.setPlaceholderText("For example, Linen trolley")
        self.weight_edit = _double_input(
            self.seed.get("weight_kg", 0.0), minimum=0.0, maximum=1_000_000.0,
            decimals=2, suffix=" kg", step=1.0,
            tooltip="Total payload weight presented to the AMR, including its trolley or container.",
        )
        self.length_edit = _double_input(
            self.seed.get("length_m", 0.0), minimum=0.0, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.width_edit = _double_input(
            self.seed.get("width_m", 0.0), minimum=0.0, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        self.height_edit = _double_input(
            self.seed.get("height_m", 0.0), minimum=0.0, maximum=100.0,
            decimals=3, suffix=" m", step=0.05,
        )
        identity_form.addRow("Payload name", self.name_edit)
        identity_form.addRow("Loaded weight", self.weight_edit)
        identity_form.addRow("Overall length", self.length_edit)
        identity_form.addRow("Overall width", self.width_edit)
        identity_form.addRow("Overall height", self.height_edit)
        physical_layout.addWidget(identity_box)

        handling_box = QGroupBox("AMR handling")
        handling_form = QFormLayout(handling_box)
        allowed_orientations = {
            str(value).strip().lower()
            for value in (
                self.seed.get(
                    "allowed_carry_orientations", ["lengthways", "sideways"]
                )
                or []
            )
        }
        orientation_widget = QWidget()
        orientation_layout = QHBoxLayout(orientation_widget)
        orientation_layout.setContentsMargins(0, 0, 0, 0)
        self.lengthways_check = QCheckBox("Lengthways")
        self.sideways_check = QCheckBox("Sideways")
        self.lengthways_check.setChecked("lengthways" in allowed_orientations)
        self.sideways_check.setChecked("sideways" in allowed_orientations)
        orientation_layout.addWidget(self.lengthways_check)
        orientation_layout.addWidget(self.sideways_check)
        orientation_layout.addStretch(1)
        self.prefer_multi_stop_amr_check = QCheckBox(
            "Prefer an AMR with several payload slots when one is available"
        )
        self.prefer_multi_stop_amr_check.setChecked(
            bool(self.seed.get("prefer_multi_stop_amr", False))
        )
        handling_form.addRow("Permitted carrying orientation", orientation_widget)
        handling_form.addRow("Multi-stop preference", self.prefer_multi_stop_amr_check)
        physical_layout.addWidget(handling_box)
        physical_layout.addWidget(
            _dialog_intro(
                "Lengthways uses the payload length along the direction of travel. Sideways swaps length and width "
                "for AMR, lift and corridor-clearance checks."
            )
        )
        physical_layout.addStretch(1)

        contents_page = QWidget()
        tabs.addTab(contents_page, "Tracked contents")
        contents_layout = QVBoxLayout(contents_page)
        self.track_items_check = QCheckBox(
            "Track stock or consumable items held inside this payload"
        )
        self.track_items_check.setChecked(bool(self.seed.get("track_items", False)))
        self.track_items_check.setToolTip(
            "Enable this for payloads whose contents deplete and trigger replenishment tasks."
        )
        contents_layout.addWidget(self.track_items_check)
        contents_layout.addWidget(
            _dialog_intro(
                "Tracked contents are optional. They can model stock levels, consumption, top-up thresholds, "
                "exchange payloads and replenishment source locations."
            )
        )
        self.items_table = QTableWidget(0, 7)
        self.items_table.setHorizontalHeaderLabels(
            [
                "Item",
                "Maximum",
                "Top-up threshold",
                "Demand pattern",
                "Consumption / day",
                "Exchange payload",
                "Source",
            ]
        )
        _configure_data_table(self.items_table)
        self.items_table.doubleClicked.connect(self.edit_tracked_item)
        contents_layout.addWidget(self.items_table, 1)
        item_buttons = QHBoxLayout()
        contents_layout.addLayout(item_buttons)
        self.add_item_btn = QPushButton("Add tracked item")
        self.edit_item_btn = QPushButton("Edit selected")
        self.delete_item_btn = QPushButton("Delete selected")
        self.add_item_btn.clicked.connect(self.add_tracked_item)
        self.edit_item_btn.clicked.connect(self.edit_tracked_item)
        self.delete_item_btn.clicked.connect(self.delete_tracked_item)
        item_buttons.addWidget(self.add_item_btn)
        item_buttons.addWidget(self.edit_item_btn)
        item_buttons.addWidget(self.delete_item_btn)
        item_buttons.addStretch(1)
        self.track_items_check.toggled.connect(self._update_contents_enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_items_table()
        self._update_contents_enabled()
        _polish_dialog(self)

    def _normalise_tracked_items(self, value):
        result = []
        if isinstance(value, dict):
            iterable = []
            for name, cfg in value.items():
                cfg = dict(cfg or {})
                cfg["name"] = name
                iterable.append(cfg)
        else:
            iterable = list(value or [])
        for item in iterable:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            result.append(
                {
                    "name": name,
                    "max": float(item.get("max", 100)),
                    "top_up_threshold": float(item.get("top_up_threshold", 15)),
                    "usage_rate": str(item.get("usage_rate", "scheduled_sporadic")),
                    "consumption_per_day": float(item.get("consumption_per_day", 0.0)),
                    "exchange_payload": str(item.get("exchange_payload", "")),
                    "source_location": str(item.get("source_location", "")),
                }
            )
        return result

    @staticmethod
    def _usage_label(value):
        labels = {mode: label for label, mode in PayloadTrackedItemDialog.MODES}
        return labels.get(str(value), str(value).replace("_", " ").title())

    def _update_contents_enabled(self):
        enabled = bool(self.track_items_check.isChecked())
        self.items_table.setEnabled(enabled)
        self.add_item_btn.setEnabled(enabled)
        self.edit_item_btn.setEnabled(enabled)
        self.delete_item_btn.setEnabled(enabled)

    def _refresh_items_table(self):
        self.items_table.setRowCount(0)
        for item in self.tracked_items:
            row = self.items_table.rowCount()
            self.items_table.insertRow(row)
            values = [
                item.get("name", ""),
                f"{float(item.get('max', 100)):g}",
                f"{float(item.get('top_up_threshold', 15)):g}",
                self._usage_label(item.get("usage_rate", "scheduled_sporadic")),
                f"{float(item.get('consumption_per_day', 0.0)):g}",
                item.get("exchange_payload", "") or "—",
                item.get("source_location", "") or "—",
            ]
            for col, value in enumerate(values):
                self.items_table.setItem(row, col, QTableWidgetItem(str(value)))

    def add_tracked_item(self):
        dialog = PayloadTrackedItemDialog(
            self,
            payload_names=self.payload_names,
            location_names=self.location_names,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            name = dialog.result["name"]
            if any(str(x.get("name", "")).strip() == name for x in self.tracked_items):
                QMessageBox.warning(self, "Duplicate tracked item", "An item with this name already exists.")
                return
            self.tracked_items.append(dialog.result)
            self._refresh_items_table()
            self.items_table.selectRow(len(self.tracked_items) - 1)

    def edit_tracked_item(self):
        row = self.items_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Tracked contents", "Select an item to edit.")
            return
        dialog = PayloadTrackedItemDialog(
            self,
            self.tracked_items[row],
            payload_names=self.payload_names,
            location_names=self.location_names,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            name = dialog.result["name"]
            for idx, item in enumerate(self.tracked_items):
                if idx != row and str(item.get("name", "")).strip() == name:
                    QMessageBox.warning(self, "Duplicate tracked item", "An item with this name already exists.")
                    return
            self.tracked_items[row] = dialog.result
            self._refresh_items_table()
            self.items_table.selectRow(row)

    def delete_tracked_item(self):
        row = self.items_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Tracked contents", "Select an item to delete.")
            return
        item_name = str(self.tracked_items[row].get("name", "tracked item"))
        if QMessageBox.question(
            self,
            "Delete tracked item",
            f"Delete '{item_name}' from this payload?",
        ) != QMessageBox.Yes:
            return
        del self.tracked_items[row]
        self._refresh_items_table()

    def accept(self):
        try:
            name = self.name_edit.text().strip()
            if not name:
                raise ValueError("Enter a payload name.")
            orientations = []
            if self.lengthways_check.isChecked():
                orientations.append("lengthways")
            if self.sideways_check.isChecked():
                orientations.append("sideways")
            if not orientations:
                raise ValueError("Select at least one permitted carrying orientation.")
            dimensions = [
                float(self.length_edit.value()),
                float(self.width_edit.value()),
                float(self.height_edit.value()),
            ]
            if any(value <= 0.0 for value in dimensions):
                raise ValueError("Payload length, width and height must all be greater than zero.")
            items_payload = {
                item["name"]: {
                    "max": float(item.get("max", 100)),
                    "top_up_threshold": float(item.get("top_up_threshold", 15)),
                    "usage_rate": str(item.get("usage_rate", "scheduled_sporadic")),
                    "consumption_per_day": float(item.get("consumption_per_day", 0.0)),
                    "exchange_payload": str(item.get("exchange_payload", "")),
                    "source_location": str(item.get("source_location", "")),
                }
                for item in self.tracked_items
            }
            self.result = {
                "name": name,
                "weight_kg": float(self.weight_edit.value()),
                "length_m": dimensions[0],
                "width_m": dimensions[1],
                "height_m": dimensions[2],
                "track_items": self.track_items_check.isChecked(),
                "prefer_multi_stop_amr": self.prefer_multi_stop_amr_check.isChecked(),
                "allowed_carry_orientations": orientations,
                "items": items_payload,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check payload details", str(exc))


class PayloadListDialog(QDialog):
    columns = [
        ("name", "Name", 180),
        ("weight_kg", "Weight kg", 90),
        ("length_m", "Length m", 90),
        ("width_m", "Width m", 90),
        ("height_m", "Height m", 90),
        ("track_items", "Track items", 90),
        ("prefer_multi_stop_amr", "Prefer multi-stop", 120),
        ("items", "Items", 260),
    ]

    def __init__(self, parent, items, on_save, location_names=None):
        super().__init__(parent)
        self.setWindowTitle("Payloads")
        self.resize(980, 520)
        self.items = [dict(x) for x in items]
        self.on_save = on_save
        self.location_names = sorted(location_names or [])

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self.edit_item())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)

        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)

        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        layout.addLayout(row)

        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        delete_btn.clicked.connect(self.delete_item)
        save_btn.clicked.connect(self.save_items)

        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(delete_btn)
        row.addStretch(1)
        row.addWidget(save_btn)

        self._refresh_table()
        _polish_dialog(self)

    def _items_summary(self, payload):
        items = payload.get("items", {})
        if isinstance(items, dict):
            names = list(items.keys())
        else:
            names = [
                str(x.get("name", "")).strip()
                for x in items or []
                if isinstance(x, dict) and str(x.get("name", "")).strip()
            ]

        if not names:
            return ""

        if len(names) <= 3:
            return ", ".join(names)

        return ", ".join(names[:3]) + f", +{len(names) - 3} items"

    def _refresh_table(self):
        self.table.setRowCount(0)

        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            values = [
                str(item.get("name", "")),
                str(item.get("weight_kg", "")),
                str(item.get("length_m", "")),
                str(item.get("width_m", "")),
                str(item.get("height_m", "")),
                "Yes" if item.get("track_items", False) else "No",
                "Yes" if item.get("prefer_multi_stop_amr", False) else "No",
                self._items_summary(item),
            ]

            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

    def _suggest_next_payload_name(self):
        existing = {str(x.get("name", "")).strip() for x in self.items}
        base = "payload"
        if base not in existing:
            return base
        counter = 2
        while f"{base}_{counter}" in existing:
            counter += 1
        return f"{base}_{counter}"

    def add_item(self):
        dialog = PayloadEditorDialog(
            self,
            seed={"name": self._suggest_next_payload_name()},
            payload_names=[x.get("name", "") for x in self.items],
            location_names=self.location_names,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            name = dialog.result["name"]
            if any(str(x.get("name", "")).strip() == name for x in self.items):
                QMessageBox.critical(self, "Duplicate", "Payload already exists")
                return
            self.items.append(dialog.result)
            self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return

        dialog = PayloadEditorDialog(
            self,
            seed=self.items[row],
            payload_names=[
                x.get("name", "") for idx, x in enumerate(self.items) if idx != row
            ],
            location_names=self.location_names,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_name = dialog.result["name"]
            for idx, item in enumerate(self.items):
                if idx != row and str(item.get("name", "")).strip() == new_name:
                    QMessageBox.critical(self, "Duplicate", "Payload already exists")
                    return

            self.items[row] = dialog.result
            self._refresh_table()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._refresh_table()

    def save_items(self):
        self.on_save(self.items)
        self.accept()


class DeliveryResourcesDialog(QDialog):
    columns = [
        ("kind", "Resource", 90),
        ("id", "Type", 150),
        ("quantity", "Qty", 65),
        ("base", "Base", 150),
        ("speed", "Speed", 85),
        ("capacity", "Payload capacity", 180),
        ("availability", "Availability", 230),
    ]

    def __init__(self, parent, amrs, staff_resources, location_names, on_save):
        super().__init__(parent)
        self.setWindowTitle("Delivery Resources")
        self.resize(1120, 540)
        self.location_names = list(location_names)
        self.on_save = on_save
        self.items = []
        for item in amrs:
            payload = dict(item)
            payload["_resource_kind"] = "amr"
            self.items.append(payload)
        for item in staff_resources:
            payload = normalise_staff_delivery_resource(item)
            payload["_resource_kind"] = "staff"
            self.items.append(payload)

        layout = QVBoxLayout(self)
        layout.addWidget(_dialog_intro(
            "AMRs and staff delivery types are configured together. Staff delivery resources "
            "transport payloads; endpoint handling staff remain in Task Generation settings."
        ))
        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels([heading for _key, heading, _width in self.columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self.edit_item())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        for text, handler in [
            ("Add AMR type", self.add_amr),
            ("Add staff type", self.add_staff),
            ("Edit selected", self.edit_item),
            ("Delete selected", self.delete_item),
        ]:
            button = QPushButton(text)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch(1)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_items)
        buttons.addWidget(save_btn)
        self._refresh_table()
        _polish_dialog(self)

    def _suggest_id(self, prefix):
        existing = {str(item.get("id", "")).strip() for item in self.items}
        index = 1
        while f"{prefix}-{index}" in existing:
            index += 1
        return f"{prefix}-{index}"

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            kind = item.get("_resource_kind", "amr")
            if kind == "staff":
                values = [
                    "Staff",
                    item.get("id", ""),
                    item.get("quantity", ""),
                    item.get("base_location", ""),
                    f"{float(item.get('speed_m_per_sec', 0.0) or 0.0):g} m/s",
                    f"{float(item.get('payload_capacity_kg', 0.0) or 0.0):g} kg",
                    f"{item.get('shift_start_time', '')}–{item.get('shift_end_time', '')}; "
                    f"{len(item.get('days_active', []))} day(s)",
                ]
            else:
                values = [
                    "AMR",
                    item.get("id", ""),
                    item.get("quantity", ""),
                    "Charging locations",
                    f"{float(item.get('speed_m_per_sec', 0.0) or 0.0):g} m/s",
                    _amr_payload_slot_summary(item),
                    "Battery and charger availability",
                ]
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def _duplicate_id(self, resource_id, skip_row=None):
        return any(
            row != skip_row and str(item.get("id", "")).strip() == resource_id
            for row, item in enumerate(self.items)
        )

    def add_amr(self):
        dialog = AMREditorDialog(
            self, self.location_names, default_amr_id=self._suggest_id("AMR")
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if self._duplicate_id(dialog.result["id"]):
                QMessageBox.critical(self, "Duplicate", "Delivery resource type already exists")
                return
            result = dict(dialog.result)
            result["_resource_kind"] = "amr"
            self.items.append(result)
            self._refresh_table()

    def add_staff(self):
        dialog = StaffDeliveryResourceEditorDialog(
            self, self.location_names, default_id=self._suggest_id("PORTER")
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if self._duplicate_id(dialog.result["id"]):
                QMessageBox.critical(self, "Duplicate", "Delivery resource type already exists")
                return
            result = dict(dialog.result)
            result["_resource_kind"] = "staff"
            self.items.append(result)
            self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.items[row]
        if item.get("_resource_kind") == "staff":
            dialog = StaffDeliveryResourceEditorDialog(self, self.location_names, seed=item)
        else:
            dialog = AMREditorDialog(self, self.location_names, seed=item)
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if self._duplicate_id(dialog.result["id"], skip_row=row):
                QMessageBox.critical(self, "Duplicate", "Delivery resource type already exists")
                return
            result = dict(dialog.result)
            result["_resource_kind"] = item.get("_resource_kind", "amr")
            self.items[row] = result
            self._refresh_table()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row >= 0:
            del self.items[row]
            self._refresh_table()

    def save_items(self):
        amrs, staff = [], []
        for item in self.items:
            payload = {key: value for key, value in item.items() if key != "_resource_kind"}
            if item.get("_resource_kind") == "staff":
                staff.append(normalise_staff_delivery_resource(payload, len(staff) + 1))
            else:
                amrs.append(payload)
        self.on_save(amrs, staff)
        self.accept()


class AMRListDialog(QDialog):
    columns = [
        ("id", "ID", 120),
        ("quantity", "Qty", 70),
        ("payload_slot_summary", "Payload slots", 180),
        ("multi_stop_enabled", "Multi-stop", 90),
        ("manual_task_compatible", "Manual task", 100),
        ("speed_m_per_sec", "Speed", 80),
        ("motor_power_w", "Motor W", 90),
        ("battery_capacity_kwh", "Battery kWh", 100),
        ("battery_charge_rate_kw", "Charge kW", 100),
        ("recharge_threshold_percent", "Recharge %", 100),
        ("battery_soc_percent", "SOC %", 80),
    ]

    def __init__(self, parent, items, location_names, on_save):
        super().__init__(parent)
        self.setWindowTitle("AMRs")
        self.resize(1200, 520)
        self.items = [dict(x) for x in items]
        self.location_names = list(location_names)
        self.on_save = on_save

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [heading for _key, heading, _width in self.columns]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self.edit_item())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)

        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)

        layout.addWidget(self.table)

        row = QHBoxLayout()
        layout.addLayout(row)

        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        delete_btn.clicked.connect(self.delete_item)
        save_btn.clicked.connect(self.save_items)

        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(delete_btn)
        row.addStretch(1)
        row.addWidget(save_btn)

        self._refresh_table()
        _polish_dialog(self)

    def _suggest_next_amr_id(self):
        nums = []
        for item in self.items:
            value = str(item.get("id", "")).strip().upper()
            if value.startswith("AMR-") and value[4:].isdigit():
                nums.append(int(value[4:]))
            elif value.startswith("AMR") and value[3:].isdigit():
                nums.append(int(value[3:]))
        return f"AMR-{max(nums, default=0) + 1}"

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, (key, _heading, _width) in enumerate(self.columns):
                if key == "payload_slot_summary":
                    value = _amr_payload_slot_summary(item)
                elif key == "manual_task_compatible":
                    value = (
                        "Yes" if len(_normalise_amr_payload_slots(item)) == 1 else "No"
                    )
                else:
                    value = item.get(key, "")
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def add_item(self):
        dialog = AMREditorDialog(
            self,
            self.location_names,
            default_amr_id=self._suggest_next_amr_id(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if any(x.get("id") == dialog.result["id"] for x in self.items):
                QMessageBox.critical(self, "Duplicate", "AMR ID already exists")
                return
            self.items.append(dialog.result)
            self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return

        dialog = AMREditorDialog(self, self.location_names, seed=self.items[row])
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_id = dialog.result["id"]
            for idx, item in enumerate(self.items):
                if idx != row and item.get("id") == new_id:
                    QMessageBox.critical(self, "Duplicate", "AMR ID already exists")
                    return
            self.items[row] = dialog.result
            self._refresh_table()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._refresh_table()

    def save_items(self):
        self.on_save(self.items)
        self.accept()


class TableListEditor(QMainWindow):
    def __init__(self, master, title, columns, items, on_save):
        super().__init__(master)
        self.setWindowTitle(title)
        self.resize(1100, 500)
        self.columns = columns
        self.items = items
        self.on_save = on_save

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels([c[1] for c in columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_, _, width) in enumerate(columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table)

        button_row = QHBoxLayout()
        layout.addLayout(button_row)
        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")
        button_row.addWidget(add_btn)
        button_row.addWidget(edit_btn)
        button_row.addWidget(delete_btn)
        button_row.addStretch(1)
        button_row.addWidget(save_btn)

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        delete_btn.clicked.connect(self.delete_item)
        save_btn.clicked.connect(self.save)

        self._refresh_table()
        self.show()
        _polish_dialog(self)

    @staticmethod
    def stringify(value: Any) -> str:
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return str(value)

    def parse_value(self, value: str):
        value = value.strip()
        if value.startswith("[") or value.startswith("{"):
            return json.loads(value)
        if value == "":
            return ""
        try:
            if "." in value:
                return float(value)
            return int(value)
        except Exception:
            return value

    def prompt_item(self, seed=None):
        seed = seed or {}
        result = {}
        for key, heading, _ in self.columns:
            value, ok = QInputDialog.getText(
                self,
                self.windowTitle(),
                heading,
                text=self.stringify(seed.get(key, "")),
            )
            if not ok:
                return None
            result[key] = self.parse_value(value)
        return result

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, (key, _heading, _width) in enumerate(self.columns):
                self.table.setItem(
                    row, col, QTableWidgetItem(self.stringify(item.get(key, "")))
                )

    def add_item(self):
        item = self.prompt_item()
        if item is None:
            return
        self.items.append(item)
        self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        updated = self.prompt_item(self.items[row])
        if updated is None:
            return
        self.items[row] = updated
        self._refresh_table()
        self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._refresh_table()

    def save(self):
        self.on_save(self.items)
        self.close()


class RouteProfilesEditor(QDialog):
    def __init__(self, master, profiles, point_names, lift_ids, on_save):
        super().__init__(master)
        QMessageBox.information(
            self,
            "Route Profiles",
            "This legacy editor is not used by the main window. Use RouteProfilesEditorV2 instead.",
        )
        self.on_save = on_save
        self.profiles = profiles
        _polish_dialog(self)



class PeopleMovementEditorDialog(QDialog):
    PROFILE_SHAPES = [
        ("Constant throughout the timeframe", "constant"),
        ("Ramp up to the peak", "ramp_up"),
        ("Ramp down from the peak", "ramp_down"),
        ("Ramp up, remain busy, then ramp down", "ramp_up_down"),
    ]
    TIMEFRAME_PRESETS = [
        ("Custom timeframe", "custom", None, None, ""),
        ("Full day", "full_day", "00:00", "00:00", "Full day"),
        ("Early shift", "early_shift", "06:00", "14:00", "Early shift"),
        ("Day shift", "day_shift", "08:00", "16:00", "Day shift"),
        ("Late shift", "late_shift", "14:00", "22:00", "Late shift"),
        ("Night shift", "night_shift", "22:00", "06:00", "Night shift"),
        ("Typical visiting period", "visiting", "14:00", "20:00", "Visiting period"),
        ("Midday meal-service period", "meal_service", "11:30", "14:00", "Meal service"),
    ]

    def __init__(
        self,
        parent,
        location_names,
        corridor_options,
        seed=None,
        initially_selected_corridors=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("People movement profile")
        self.resize(880, 720)
        self.setMinimumSize(700, 560)
        self.result = None
        seed = dict(seed or {})
        self.location_names = sorted(set(str(x) for x in location_names if str(x)))
        self.corridor_options = sorted(set(str(x) for x in corridor_options if str(x)))
        seeded_corridors = list(seed.get("corridor_edges", []) or [])
        if initially_selected_corridors:
            seeded_corridors = list(initially_selected_corridors)
        self.selected_corridors = [
            x for x in self.corridor_options if x in set(str(v) for v in seeded_corridors)
        ]

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Define when and where people use the graph. The profile can remain constant, "
                "ramp up, ramp down, or follow a busy-period shape within a named timeframe. "
                "The preview shows the people represented by each movement interval."
            )
        )

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_profile_tab(seed)
        self._build_route_tab(seed)
        self._build_flow_tab(seed)
        self._build_interaction_tab(seed)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_corridor_summary()
        self._update_route_mode()
        self._update_shape_controls()
        self._update_preview()
        _polish_dialog(self)

    @staticmethod
    def _time_from_text(value, fallback):
        text = str(value or fallback)
        if text == "24:00":
            text = "00:00"
        parsed = QTime.fromString(text, "HH:mm")
        return parsed if parsed.isValid() else QTime.fromString(fallback, "HH:mm")

    def _build_profile_tab(self, seed):
        page = QWidget()
        self.tabs.addTab(page, "Profile and timeframe")
        layout = QVBoxLayout(page)

        identity = QGroupBox("Profile identity")
        identity_form = QFormLayout(identity)
        self.id_edit = QLineEdit(str(seed.get("id", "PEOPLE-1")))
        self.id_edit.setPlaceholderText("For example, PUBLIC-VISITING")
        self.id_edit.setToolTip("A unique name used when assigning this profile to corridor assets.")
        self.enabled_check = QCheckBox("Use this profile in simulations")
        self.enabled_check.setChecked(bool(seed.get("enabled", True)))
        self.group_combo = QComboBox()
        self.group_combo.addItem("Staff only", "staff")
        self.group_combo.addItem("Public only", "public")
        self.group_combo.addItem("Mixed staff and public", "both")
        group = str(seed.get("group_type", "staff") or "staff").lower()
        if group == "mixed":
            group = "both"
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(group)))
        identity_form.addRow("Profile name", self.id_edit)
        identity_form.addRow("People using the route", self.group_combo)
        identity_form.addRow("Status", self.enabled_check)
        layout.addWidget(identity)

        timeframe = QGroupBox("Daily timeframe")
        timeframe_form = QFormLayout(timeframe)
        self.timeframe_preset_combo = QComboBox()
        for label, value, _start, _end, _name in self.TIMEFRAME_PRESETS:
            self.timeframe_preset_combo.addItem(label, value)
        preset = str(seed.get("timeframe_preset", "custom") or "custom").lower()
        self.timeframe_preset_combo.setCurrentIndex(
            max(0, self.timeframe_preset_combo.findData(preset))
        )
        self.timeframe_name_edit = QLineEdit(str(seed.get("timeframe_name", "") or ""))
        self.timeframe_name_edit.setPlaceholderText("For example, morning arrival peak")
        self.start_time_edit = QTimeEdit()
        self.start_time_edit.setDisplayFormat("HH:mm")
        self.start_time_edit.setTime(self._time_from_text(seed.get("start_time"), "08:00"))
        self.end_time_edit = QTimeEdit()
        self.end_time_edit.setDisplayFormat("HH:mm")
        self.end_time_edit.setTime(self._time_from_text(seed.get("end_time"), "18:00"))
        self.day_selector = DayOfWeekSelector(
            selected=seed.get("days_active", ["mon", "tue", "wed", "thu", "fri"])
        )
        timeframe_form.addRow("Preset", self.timeframe_preset_combo)
        timeframe_form.addRow("Timeframe description", self.timeframe_name_edit)
        timeframe_form.addRow("Starts", self.start_time_edit)
        timeframe_form.addRow("Ends", self.end_time_edit)
        timeframe_form.addRow("Active days", self.day_selector)
        layout.addWidget(timeframe)
        layout.addStretch(1)

        self.timeframe_preset_combo.currentIndexChanged.connect(self._apply_timeframe_preset)
        self.start_time_edit.timeChanged.connect(self._mark_custom_timeframe)
        self.end_time_edit.timeChanged.connect(self._mark_custom_timeframe)
        self.start_time_edit.timeChanged.connect(self._update_preview)
        self.end_time_edit.timeChanged.connect(self._update_preview)

    def _build_route_tab(self, seed):
        page = QWidget()
        self.tabs.addTab(page, "Route or corridor assets")
        layout = QVBoxLayout(page)

        route_box = QGroupBox("How the profile is applied")
        form = QFormLayout(route_box)
        self.route_mode_combo = QComboBox()
        self.route_mode_combo.addItem("Route people between two locations", "route")
        self.route_mode_combo.addItem("Apply the profile directly to selected corridors", "corridors")
        self.route_mode_combo.setCurrentIndex(1 if self.selected_corridors else 0)
        self.start_combo = QComboBox()
        self.start_combo.setEditable(False)
        self.start_combo.addItem("Select a start location…", "")
        for name in self.location_names:
            self.start_combo.addItem(name, name)
        self.end_combo = QComboBox()
        self.end_combo.addItem("Select an end location…", "")
        for name in self.location_names:
            self.end_combo.addItem(name, name)
        start = str(seed.get("start_location", "") or "")
        end = str(seed.get("end_location", "") or "")
        self.start_combo.setCurrentIndex(max(0, self.start_combo.findData(start)))
        self.end_combo.setCurrentIndex(max(0, self.end_combo.findData(end)))

        corridor_widget = QWidget()
        corridor_layout = QVBoxLayout(corridor_widget)
        corridor_layout.setContentsMargins(0, 0, 0, 0)
        self.corridor_summary = QLabel()
        self.corridor_summary.setWordWrap(True)
        corridor_actions = QHBoxLayout()
        self.corridor_button = QPushButton("Select corridor assets…")
        self.corridor_button.clicked.connect(self.select_corridors)
        clear_button = QPushButton("Clear selection")
        clear_button.clicked.connect(self.clear_corridors)
        corridor_actions.addWidget(self.corridor_button)
        corridor_actions.addWidget(clear_button)
        corridor_actions.addStretch(1)
        corridor_layout.addWidget(self.corridor_summary)
        corridor_layout.addLayout(corridor_actions)

        form.addRow("Application method", self.route_mode_combo)
        form.addRow("Start location", self.start_combo)
        form.addRow("End location", self.end_combo)
        form.addRow("Corridor assets", corridor_widget)
        layout.addWidget(route_box)
        layout.addWidget(
            _dialog_intro(
                "Location routing calculates the path through the graph. Direct corridor assignment "
                "is better for observed occupancy, public routes, waiting areas, and scenario studies."
            )
        )
        layout.addStretch(1)
        self.route_mode_combo.currentIndexChanged.connect(self._update_route_mode)

    def _build_flow_tab(self, seed):
        page = QWidget()
        self.tabs.addTab(page, "Flow profile")
        layout = QVBoxLayout(page)

        settings = QGroupBox("People flow")
        form = QFormLayout(settings)
        self.shape_combo = QComboBox()
        for label, value in self.PROFILE_SHAPES:
            self.shape_combo.addItem(label, value)
        shape = str(seed.get("profile_shape", "constant") or "constant").lower()
        self.shape_combo.setCurrentIndex(max(0, self.shape_combo.findData(shape)))

        self.minimum_people_spin = QSpinBox()
        self.minimum_people_spin.setRange(0, 100000)
        self.minimum_people_spin.setValue(max(0, int(seed.get("minimum_people_per_interval", 0) or 0)))
        self.minimum_people_spin.setSuffix(" people")
        self.minimum_people_spin.setToolTip("People represented at the quietest point of the profile.")

        self.peak_people_spin = QSpinBox()
        self.peak_people_spin.setRange(1, 100000)
        self.peak_people_spin.setValue(max(1, int(float(seed.get("people_per_trip", 1) or 1))))
        self.peak_people_spin.setSuffix(" people")
        self.peak_people_spin.setToolTip("Maximum people represented by one movement interval.")

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 1440.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setValue(max(0.1, float(seed.get("interval_minutes", 15.0) or 15.0)))
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setToolTip("How often a group enters the route during the active timeframe.")

        self.ramp_up_spin = QDoubleSpinBox()
        self.ramp_up_spin.setRange(0.0, 1440.0)
        self.ramp_up_spin.setDecimals(1)
        self.ramp_up_spin.setValue(max(0.0, float(seed.get("ramp_up_minutes", 60.0) or 0.0)))
        self.ramp_up_spin.setSuffix(" min")
        self.ramp_down_spin = QDoubleSpinBox()
        self.ramp_down_spin.setRange(0.0, 1440.0)
        self.ramp_down_spin.setDecimals(1)
        self.ramp_down_spin.setValue(max(0.0, float(seed.get("ramp_down_minutes", 60.0) or 0.0)))
        self.ramp_down_spin.setSuffix(" min")
        self.fit_profile_check = QCheckBox(
            "Fit the ramp automatically to the full daily timeframe"
        )
        existing_profile = any(
            key in seed
            for key in (
                "profile_shape",
                "people_per_trip",
                "start_time",
                "end_time",
                "corridor_edges",
                "start_location",
                "end_location",
            )
        )
        self.fit_profile_check.setChecked(
            bool(seed.get("fit_profile_to_timeframe", not existing_profile))
        )
        self.fit_profile_check.setToolTip(
            "Ramp up reaches the peak at the end of the timeframe; ramp down starts at the peak and reaches "
            "the quiet value at the end; ramp up/down reaches the peak at the midpoint."
        )

        form.addRow("Profile shape", self.shape_combo)
        form.addRow("Quiet-period people per interval", self.minimum_people_spin)
        form.addRow("Peak people per interval", self.peak_people_spin)
        form.addRow("Movement interval", self.interval_spin)
        form.addRow("Timeframe alignment", self.fit_profile_check)
        form.addRow("Manual ramp-up duration", self.ramp_up_spin)
        form.addRow("Manual ramp-down duration", self.ramp_down_spin)
        layout.addWidget(settings)

        preview_box = QGroupBox("Profile preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview = PeopleProfilePreview()
        self.preview_summary = QLabel()
        self.preview_summary.setWordWrap(True)
        preview_layout.addWidget(self.preview)
        preview_layout.addWidget(self.preview_summary)
        layout.addWidget(preview_box)
        layout.addStretch(1)

        for widget in [
            self.shape_combo,
            self.minimum_people_spin,
            self.peak_people_spin,
            self.interval_spin,
            self.ramp_up_spin,
            self.ramp_down_spin,
            self.fit_profile_check,
        ]:
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._update_preview)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._update_preview)
            else:
                widget.valueChanged.connect(self._update_preview)
        self.shape_combo.currentIndexChanged.connect(self._update_shape_controls)
        self.fit_profile_check.toggled.connect(self._update_shape_controls)

    def _build_interaction_tab(self, seed):
        page = QWidget()
        self.tabs.addTab(page, "AMR interaction")
        layout = QVBoxLayout(page)
        box = QGroupBox("Movement and AMR safety effect")
        form = QFormLayout(box)
        self.walk_speed_spin = QDoubleSpinBox()
        self.walk_speed_spin.setRange(0.1, 3.0)
        self.walk_speed_spin.setDecimals(2)
        self.walk_speed_spin.setValue(max(0.1, float(seed.get("walking_speed_m_per_sec", 1.2) or 1.2)))
        self.walk_speed_spin.setSuffix(" m/s")
        self.walk_speed_spin.setToolTip("Average walking speed used to reserve each corridor segment.")
        self.amr_speed_percent_spin = QDoubleSpinBox()
        self.amr_speed_percent_spin.setRange(5.0, 100.0)
        self.amr_speed_percent_spin.setDecimals(0)
        factor = max(0.05, min(1.0, float(seed.get("amr_speed_factor", 0.7) or 0.7)))
        self.amr_speed_percent_spin.setValue(factor * 100.0)
        self.amr_speed_percent_spin.setSuffix(" %")
        self.amr_speed_percent_spin.setToolTip(
            "Maximum AMR speed while this people profile overlaps the route. "
            "Additional crowding slowdown is applied from the people count."
        )
        form.addRow("Average walking speed", self.walk_speed_spin)
        form.addRow("AMR speed while sharing the route", self.amr_speed_percent_spin)
        layout.addWidget(box)
        layout.addWidget(
            _dialog_intro(
                "For example, 70% means an AMR normally travelling at 1.0 m/s is limited to "
                "0.7 m/s while this profile is present. Higher people counts may reduce it further."
            )
        )
        layout.addStretch(1)

    def _apply_timeframe_preset(self):
        preset_value = str(self.timeframe_preset_combo.currentData() or "custom")
        match = next((item for item in self.TIMEFRAME_PRESETS if item[1] == preset_value), None)
        if not match or preset_value == "custom":
            return
        _label, _value, start, end, default_name = match
        self.start_time_edit.blockSignals(True)
        self.end_time_edit.blockSignals(True)
        self.start_time_edit.setTime(self._time_from_text(start, "00:00"))
        self.end_time_edit.setTime(self._time_from_text(end, "00:00"))
        self.start_time_edit.blockSignals(False)
        self.end_time_edit.blockSignals(False)
        if not self.timeframe_name_edit.text().strip() or self.timeframe_name_edit.text().strip() in {
            item[4] for item in self.TIMEFRAME_PRESETS if item[4]
        }:
            self.timeframe_name_edit.setText(default_name)
        self._update_preview()

    def _mark_custom_timeframe(self):
        current = str(self.timeframe_preset_combo.currentData() or "custom")
        if current != "custom":
            self.timeframe_preset_combo.blockSignals(True)
            self.timeframe_preset_combo.setCurrentIndex(
                max(0, self.timeframe_preset_combo.findData("custom"))
            )
            self.timeframe_preset_combo.blockSignals(False)

    def _update_route_mode(self):
        direct = str(self.route_mode_combo.currentData() or "route") == "corridors"
        self.start_combo.setEnabled(not direct)
        self.end_combo.setEnabled(not direct)
        self.corridor_button.setEnabled(direct)

    def _timeframe_minutes(self):
        start = self.start_time_edit.time().hour() * 60 + self.start_time_edit.time().minute()
        end = self.end_time_edit.time().hour() * 60 + self.end_time_edit.time().minute()
        if end <= start:
            end += 24 * 60
        return max(0.1, float(end - start))

    def _effective_ramps(self, shape):
        total = self._timeframe_minutes()
        if self.fit_profile_check.isChecked():
            if shape == "ramp_up":
                return total, 0.0
            if shape == "ramp_down":
                return 0.0, total
            if shape == "ramp_up_down":
                return total / 2.0, total / 2.0
        return float(self.ramp_up_spin.value()), float(self.ramp_down_spin.value())

    def _update_shape_controls(self):
        shape = str(self.shape_combo.currentData() or "constant")
        manual = not self.fit_profile_check.isChecked()
        self.fit_profile_check.setEnabled(shape != "constant")
        self.ramp_up_spin.setEnabled(manual and shape in {"ramp_up", "ramp_up_down"})
        self.ramp_down_spin.setEnabled(manual and shape in {"ramp_down", "ramp_up_down"})
        self.minimum_people_spin.setEnabled(shape != "constant")
        self._update_preview()

    def _update_preview(self):
        if not hasattr(self, "preview"):
            return
        minimum = self.minimum_people_spin.value()
        peak = self.peak_people_spin.value()
        if minimum > peak:
            minimum = peak
        start = self.start_time_edit.time().toString("HH:mm")
        end = self.end_time_edit.time().toString("HH:mm")
        shape = str(self.shape_combo.currentData() or "constant")
        ramp_up, ramp_down = self._effective_ramps(shape)
        self.preview.set_profile(
            shape=shape,
            minimum=minimum,
            peak=peak,
            ramp_up=ramp_up,
            ramp_down=ramp_down,
            start=start,
            end=end,
        )
        shape_label = self.shape_combo.currentText()
        alignment = (
            "The ramp is fitted to the complete timeframe."
            if self.fit_profile_check.isChecked() and shape != "constant"
            else "Manual ramp durations are used."
        )
        self.preview_summary.setText(
            f"{shape_label}. Groups enter every {self.interval_spin.value():g} minutes, "
            f"ranging from {minimum} to {peak} people per interval. {alignment}"
        )

    def _update_corridor_summary(self):
        count = len(self.selected_corridors)
        if not count:
            self.corridor_summary.setText("No corridor assets selected.")
        elif count <= 3:
            self.corridor_summary.setText("Selected: " + "; ".join(self.selected_corridors))
        else:
            self.corridor_summary.setText(
                f"{count} corridor assets selected: " + "; ".join(self.selected_corridors[:3]) + "…"
            )

    def select_corridors(self):
        dialog = MultiSelectPicker(
            self,
            "Select corridor assets for this people profile",
            self.corridor_options,
            selected=self.selected_corridors,
            group_resolver=lambda value: value.split(" -> ", 1)[0][:1].upper() or "Corridors",
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.selected_corridors = list(dialog.result)
            self._update_corridor_summary()

    def clear_corridors(self):
        self.selected_corridors = []
        self._update_corridor_summary()

    def accept(self):
        try:
            profile_id = self.id_edit.text().strip()
            if not profile_id:
                raise ValueError("Enter a profile name.")
            direct = str(self.route_mode_combo.currentData() or "route") == "corridors"
            start = str(self.start_combo.currentData() or "").strip()
            end = str(self.end_combo.currentData() or "").strip()
            if direct and not self.selected_corridors:
                self.tabs.setCurrentIndex(1)
                raise ValueError("Select at least one corridor asset for direct corridor use.")
            if not direct and (not start or not end or start == end):
                self.tabs.setCurrentIndex(1)
                raise ValueError("Choose different start and end locations for the people route.")
            days = self.day_selector.selected_days()
            if not days:
                self.tabs.setCurrentIndex(0)
                raise ValueError("Select at least one active day.")
            peak = int(self.peak_people_spin.value())
            minimum = int(self.minimum_people_spin.value())
            if minimum > peak:
                self.tabs.setCurrentIndex(2)
                raise ValueError("Quiet-period people cannot exceed the peak people value.")
            shape = str(self.shape_combo.currentData() or "constant")
            self.result = {
                "id": profile_id,
                "enabled": self.enabled_check.isChecked(),
                "group_type": str(self.group_combo.currentData()),
                "start_location": "" if direct else start,
                "end_location": "" if direct else end,
                "corridor_edges": list(self.selected_corridors) if direct else [],
                "people_per_trip": peak,
                "minimum_people_per_interval": minimum if shape != "constant" else peak,
                "profile_shape": shape,
                "timeframe_name": self.timeframe_name_edit.text().strip(),
                "timeframe_preset": str(self.timeframe_preset_combo.currentData() or "custom"),
                "start_time": self.start_time_edit.time().toString("HH:mm"),
                "end_time": self.end_time_edit.time().toString("HH:mm"),
                "ramp_up_minutes": float(self.ramp_up_spin.value()),
                "ramp_down_minutes": float(self.ramp_down_spin.value()),
                "fit_profile_to_timeframe": bool(self.fit_profile_check.isChecked()),
                "interval_minutes": float(self.interval_spin.value()),
                "walking_speed_m_per_sec": float(self.walk_speed_spin.value()),
                "amr_speed_factor": float(self.amr_speed_percent_spin.value()) / 100.0,
                "days_active": days,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check people movement profile", str(exc))


class PeopleMovementListDialog(QDialog):
    def __init__(
        self,
        parent,
        location_names,
        corridor_options,
        movements,
        initially_selected_corridors=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("People movement profiles")
        self.resize(1240, 640)
        self.setMinimumSize(760, 480)
        self.location_names = list(location_names)
        self.corridor_options = list(corridor_options)
        self.initially_selected_corridors = list(initially_selected_corridors or [])
        self.movements = [dict(x) for x in movements or []]
        self.result = None
        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Profiles describe people flow through locations or selected corridors. "
                "Use a ramp profile for arrival and departure peaks, and a constant profile for steady occupancy."
            )
        )
        self.summary_label = QLabel()
        layout.addWidget(self.summary_label)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "Profile",
                "Enabled",
                "People",
                "Route / corridors",
                "Timeframe",
                "Shape",
                "People per interval",
                "Interval",
                "Walking speed",
                "AMR speed",
            ]
        )
        _configure_data_table(self.table)
        self.table.doubleClicked.connect(self.edit_item)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        layout.addLayout(row)
        actions = [
            ("Add profile", self.add_item),
            ("Add from topology selection", self.add_from_topology),
            ("Edit", self.edit_item),
            ("Duplicate", self.duplicate_item),
            ("Enable / disable", self.toggle_enabled),
            ("Delete", self.delete_item),
        ]
        for text, fn in actions:
            button = QPushButton(text)
            button.clicked.connect(fn)
            if text == "Add from topology selection":
                button.setEnabled(bool(self.initially_selected_corridors))
                button.setToolTip("Uses the corridor assets currently selected in the topology view.")
            row.addWidget(button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        self.refresh()
        _polish_dialog(self)

    @staticmethod
    def _shape_label(value):
        return {
            "constant": "Constant",
            "ramp_up": "Ramp up",
            "ramp_down": "Ramp down",
            "ramp_up_down": "Ramp up / down",
        }.get(str(value or "constant"), "Constant")

    @staticmethod
    def _people_label(value):
        return {"staff": "Staff", "public": "Public", "both": "Mixed"}.get(
            str(value or "staff"), "Staff"
        )

    def refresh(self):
        self.table.setRowCount(0)
        enabled_count = 0
        for item in self.movements:
            row = self.table.rowCount()
            self.table.insertRow(row)
            corridors = list(item.get("corridor_edges", []) or [])
            if corridors:
                route_text = f"{len(corridors)} selected corridor(s)"
            else:
                route_text = f"{item.get('start_location', '')} → {item.get('end_location', '')}"
            peak = max(1, int(float(item.get("people_per_trip", 1) or 1)))
            minimum = max(0, int(float(item.get("minimum_people_per_interval", peak) or 0)))
            shape = str(item.get("profile_shape", "constant") or "constant")
            people_range = str(peak) if shape == "constant" else f"{minimum} → {peak}"
            timeframe_name = str(item.get("timeframe_name", "") or "").strip()
            time_text = f"{item.get('start_time', '')}–{item.get('end_time', '')}"
            if timeframe_name:
                time_text = f"{timeframe_name}: {time_text}"
            enabled = bool(item.get("enabled", True))
            enabled_count += 1 if enabled else 0
            values = [
                item.get("id", ""),
                "Yes" if enabled else "No",
                self._people_label(item.get("group_type")),
                route_text,
                time_text,
                (
                    self._shape_label(shape) + " (fit to timeframe)"
                    if bool(item.get("fit_profile_to_timeframe", False)) and shape != "constant"
                    else self._shape_label(shape)
                ),
                people_range,
                f"{float(item.get('interval_minutes', 15) or 15):g} min",
                f"{float(item.get('walking_speed_m_per_sec', 1.2) or 1.2):g} m/s",
                f"{float(item.get('amr_speed_factor', 0.7) or 0.7) * 100:g}%",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.summary_label.setText(
            f"{len(self.movements)} profile(s), {enabled_count} enabled. "
            "Double-click a row to edit it."
        )

    def selected_index(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _open_editor(self, seed=None, selected_corridors=None):
        return PeopleMovementEditorDialog(
            self,
            self.location_names,
            self.corridor_options,
            seed=seed,
            initially_selected_corridors=selected_corridors,
        )

    def _next_profile_id(self, base="PEOPLE"):
        existing = {str(item.get("id", "")) for item in self.movements}
        index = 1
        while f"{base}-{index}" in existing:
            index += 1
        return f"{base}-{index}"

    def add_item(self):
        dialog = self._open_editor({"id": self._next_profile_id()})
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.movements.append(dialog.result)
            self.refresh()
            self.table.selectRow(len(self.movements) - 1)

    def add_from_topology(self):
        dialog = self._open_editor(
            {"id": self._next_profile_id()},
            selected_corridors=self.initially_selected_corridors,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.movements.append(dialog.result)
            self.refresh()
            self.table.selectRow(len(self.movements) - 1)

    def edit_item(self, *_args):
        index = self.selected_index()
        if index < 0:
            QMessageBox.information(self, "People movement", "Select a profile to edit.")
            return
        dialog = self._open_editor(self.movements[index])
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.movements[index] = dialog.result
            self.refresh()
            self.table.selectRow(index)

    def duplicate_item(self):
        index = self.selected_index()
        if index < 0:
            QMessageBox.information(self, "People movement", "Select a profile to duplicate.")
            return
        clone = dict(self.movements[index])
        base = str(clone.get("id", "PEOPLE")).rstrip("-0123456789") or "PEOPLE"
        clone["id"] = self._next_profile_id(base)
        self.movements.append(clone)
        self.refresh()
        self.table.selectRow(len(self.movements) - 1)

    def toggle_enabled(self):
        index = self.selected_index()
        if index < 0:
            QMessageBox.information(self, "People movement", "Select a profile first.")
            return
        self.movements[index]["enabled"] = not bool(self.movements[index].get("enabled", True))
        self.refresh()
        self.table.selectRow(index)

    def delete_item(self):
        index = self.selected_index()
        if index < 0:
            QMessageBox.information(self, "People movement", "Select a profile to delete.")
            return
        name = str(self.movements[index].get("id", "this profile"))
        answer = QMessageBox.question(
            self,
            "Delete people movement profile",
            f"Delete {name}? Corridor assignments to this profile will be removed when the dialog is saved.",
        )
        if answer == QMessageBox.Yes:
            del self.movements[index]
            self.refresh()

    def accept(self):
        ids = [str(item.get("id", "")).strip() for item in self.movements]
        duplicates = sorted({value for value in ids if value and ids.count(value) > 1})
        if duplicates:
            QMessageBox.warning(
                self,
                "Duplicate profile names",
                "Each profile needs a unique name. Duplicates: " + ", ".join(duplicates),
            )
            return
        self.result = self.movements
        super().accept()


class ScenarioEventDialog(QDialog):
    PRESETS = [
        ("Complete outage", "outage", 0.0, 100.0),
        ("Half capacity / intermittent availability", "half_capacity", 50.0, 100.0),
        ("Slow operation", "slow", 100.0, 50.0),
        ("Limited availability and slow operation", "limited_slow", 50.0, 50.0),
        ("Custom", "custom", None, None),
    ]

    def __init__(self, parent, resource_options, seed=None):
        super().__init__(parent)
        self.setWindowTitle("Scenario event")
        self.resize(760, 620)
        self.setMinimumSize(640, 520)
        self.result = None
        seed = dict(seed or {})
        self._resource_options = resource_options
        self.selected_resources = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "A scenario event changes one or more assets during a defined daily period. "
                "Use 0% availability for a complete outage, or reduce speed to model degraded operation."
            )
        )

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        asset_page = QWidget()
        tabs.addTab(asset_page, "Assets and effect")
        asset_layout = QVBoxLayout(asset_page)
        asset_box = QGroupBox("Affected assets")
        asset_form = QFormLayout(asset_box)
        self.type_combo = QComboBox()
        for label, value in [
            ("Lifts", "lift"),
            ("Corridor edges", "corridor"),
            ("Corridor nodes and doors", "corridor_node"),
            ("AMRs or AMR types", "amr"),
        ]:
            self.type_combo.addItem(label, value)
        idx = self.type_combo.findData(str(seed.get("resource_type", "lift")))
        self.type_combo.setCurrentIndex(max(0, idx))

        resource_widget = QWidget()
        resource_layout = QVBoxLayout(resource_widget)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        self.resource_summary = QLabel()
        self.resource_summary.setWordWrap(True)
        resource_actions = QHBoxLayout()
        select_button = QPushButton("Select assets…")
        select_button.clicked.connect(self.select_resources)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_resources)
        resource_actions.addWidget(select_button)
        resource_actions.addWidget(clear_button)
        resource_actions.addStretch(1)
        resource_layout.addWidget(self.resource_summary)
        resource_layout.addLayout(resource_actions)
        asset_form.addRow("Asset type", self.type_combo)
        asset_form.addRow("Selected assets", resource_widget)
        asset_layout.addWidget(asset_box)

        effect_box = QGroupBox("Operational effect")
        effect_form = QFormLayout(effect_box)
        self.preset_combo = QComboBox()
        for label, value, _availability, _speed in self.PRESETS:
            self.preset_combo.addItem(label, value)
        self.availability_spin = QDoubleSpinBox()
        self.availability_spin.setRange(0.0, 100.0)
        self.availability_spin.setDecimals(0)
        self.availability_spin.setSuffix(" %")
        self.availability_spin.setValue(
            max(0.0, min(100.0, float(seed.get("availability_percent", 0.0) or 0.0)))
        )
        self.availability_spin.setToolTip(
            "0% makes the selected assets unavailable. Values between 0% and 100% reduce effective capacity."
        )
        self.speed_percent_spin = QDoubleSpinBox()
        self.speed_percent_spin.setRange(0.0, 100.0)
        self.speed_percent_spin.setDecimals(0)
        self.speed_percent_spin.setSuffix(" %")
        self.speed_percent_spin.setValue(
            max(0.0, min(100.0, float(seed.get("speed_factor", 1.0) or 0.0) * 100.0))
        )
        self.speed_percent_spin.setToolTip(
            "100% is normal speed. 50% doubles travel or lift movement time while the event is active."
        )
        self.effect_summary = QLabel()
        self.effect_summary.setWordWrap(True)
        effect_form.addRow("Common event", self.preset_combo)
        effect_form.addRow("Availability", self.availability_spin)
        effect_form.addRow("Operating speed", self.speed_percent_spin)
        effect_form.addRow("Result", self.effect_summary)
        asset_layout.addWidget(effect_box)
        asset_layout.addStretch(1)

        schedule_page = QWidget()
        tabs.addTab(schedule_page, "Schedule and notes")
        schedule_layout = QVBoxLayout(schedule_page)
        schedule_box = QGroupBox("Daily schedule")
        schedule_form = QFormLayout(schedule_box)
        self.start_time_edit = QTimeEdit()
        self.start_time_edit.setDisplayFormat("HH:mm")
        self.start_time_edit.setTime(self._time_from_text(seed.get("start_time"), "00:00"))
        self.end_time_edit = QTimeEdit()
        self.end_time_edit.setDisplayFormat("HH:mm")
        self.end_time_edit.setTime(self._time_from_text(seed.get("end_time"), "00:00"))
        self.day_selector = DayOfWeekSelector(
            selected=seed.get(
                "days_active", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            )
        )
        schedule_form.addRow("Starts", self.start_time_edit)
        schedule_form.addRow("Ends", self.end_time_edit)
        schedule_form.addRow("Active days", self.day_selector)
        schedule_layout.addWidget(schedule_box)

        notes_box = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_box)
        self.notes_edit = QPlainTextEdit(str(seed.get("notes", "") or ""))
        self.notes_edit.setPlaceholderText(
            "Explain the cause or assumption, for example planned lift maintenance or a corridor closure."
        )
        self.notes_edit.setMaximumHeight(130)
        notes_layout.addWidget(self.notes_edit)
        schedule_layout.addWidget(notes_box)
        schedule_layout.addWidget(
            _dialog_intro(
                "When start and end are the same, the event applies for the whole active day. "
                "An end time earlier than the start time creates an overnight event."
            )
        )
        schedule_layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        resource_ids = seed.get("resource_ids", [])
        if isinstance(resource_ids, str):
            resource_ids = [x.strip() for x in resource_ids.split(",") if x.strip()]
        legacy = str(seed.get("resource_id", "") or "").strip()
        if legacy and legacy not in resource_ids:
            resource_ids = [legacy] + list(resource_ids)
        options = set(self._resource_options.get(str(self.type_combo.currentData()), []))
        self.selected_resources = [x for x in resource_ids if x in options]

        self.type_combo.currentIndexChanged.connect(self._resource_type_changed)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        self.availability_spin.valueChanged.connect(self._mark_custom_preset)
        self.speed_percent_spin.valueChanged.connect(self._mark_custom_preset)
        self.availability_spin.valueChanged.connect(self._update_effect_summary)
        self.speed_percent_spin.valueChanged.connect(self._update_effect_summary)
        self._match_initial_preset()
        self._update_resource_summary()
        self._update_effect_summary()
        _polish_dialog(self)

    @staticmethod
    def _time_from_text(value, fallback):
        text = str(value or fallback)
        if text == "24:00":
            text = "00:00"
        parsed = QTime.fromString(text, "HH:mm")
        return parsed if parsed.isValid() else QTime.fromString(fallback, "HH:mm")

    def _match_initial_preset(self):
        availability = round(self.availability_spin.value(), 3)
        speed = round(self.speed_percent_spin.value(), 3)
        match = "custom"
        for _label, value, preset_availability, preset_speed in self.PRESETS:
            if preset_availability is None:
                continue
            if abs(availability - preset_availability) < 1e-6 and abs(speed - preset_speed) < 1e-6:
                match = value
                break
        self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(match)))

    def _apply_preset(self):
        selected = str(self.preset_combo.currentData() or "custom")
        preset = next((item for item in self.PRESETS if item[1] == selected), None)
        if not preset or preset[2] is None:
            return
        self.availability_spin.blockSignals(True)
        self.speed_percent_spin.blockSignals(True)
        self.availability_spin.setValue(float(preset[2]))
        self.speed_percent_spin.setValue(float(preset[3]))
        self.availability_spin.blockSignals(False)
        self.speed_percent_spin.blockSignals(False)
        self._update_effect_summary()

    def _mark_custom_preset(self):
        current = str(self.preset_combo.currentData() or "custom")
        preset = next((item for item in self.PRESETS if item[1] == current), None)
        if not preset or preset[2] is None:
            return
        if (
            abs(self.availability_spin.value() - float(preset[2])) > 1e-6
            or abs(self.speed_percent_spin.value() - float(preset[3])) > 1e-6
        ):
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData("custom")))
            self.preset_combo.blockSignals(False)

    def _update_effect_summary(self):
        availability = self.availability_spin.value()
        speed = self.speed_percent_spin.value()
        if availability <= 0.0:
            text = "Selected assets are unavailable for the entire event window."
        elif speed <= 0.0:
            text = "Selected assets remain available but cannot move during the event window."
        elif availability < 100.0 and speed < 100.0:
            text = f"Capacity is limited to {availability:g}% and operating speed to {speed:g}%."
        elif availability < 100.0:
            text = f"Effective capacity is limited to {availability:g}%."
        elif speed < 100.0:
            text = f"Operating speed is limited to {speed:g}% of normal."
        else:
            text = "No operational reduction is configured."
        self.effect_summary.setText(text)

    def _resource_type_changed(self):
        options = set(self._resource_options.get(str(self.type_combo.currentData()), []))
        self.selected_resources = [x for x in self.selected_resources if x in options]
        self._update_resource_summary()

    def _refresh_resources(self):
        """Backwards-compatible alias retained for existing integrations."""
        self._resource_type_changed()

    def _update_resource_summary(self):
        if not self.selected_resources:
            self.resource_summary.setText("No assets selected.")
        elif len(self.selected_resources) <= 3:
            self.resource_summary.setText("Selected: " + "; ".join(self.selected_resources))
        else:
            self.resource_summary.setText(
                f"{len(self.selected_resources)} assets selected: "
                + "; ".join(self.selected_resources[:3])
                + "…"
            )

    def select_resources(self):
        resource_type = str(self.type_combo.currentData())
        options = self._resource_options.get(resource_type, [])
        dialog = MultiSelectPicker(
            self,
            f"Select {self.type_combo.currentText().lower()}",
            options,
            selected=self.selected_resources,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.selected_resources = list(dialog.result)
            self._update_resource_summary()

    def clear_resources(self):
        self.selected_resources = []
        self._update_resource_summary()

    def accept(self):
        if not self.selected_resources:
            QMessageBox.warning(self, "Check scenario event", "Select one or more affected assets.")
            return
        days = self.day_selector.selected_days()
        if not days:
            QMessageBox.warning(self, "Check scenario event", "Select at least one active day.")
            return
        self.result = {
            "resource_type": str(self.type_combo.currentData()),
            "resource_ids": list(self.selected_resources),
            "resource_id": self.selected_resources[0],
            "start_time": self.start_time_edit.time().toString("HH:mm"),
            "end_time": self.end_time_edit.time().toString("HH:mm"),
            "availability_percent": float(self.availability_spin.value()),
            "speed_factor": float(self.speed_percent_spin.value()) / 100.0,
            "days_active": days,
            "notes": self.notes_edit.toPlainText().strip(),
        }
        super().accept()


class ScenarioTestingDialog(QDialog):
    RESOURCE_LABELS = {
        "lift": "Lift",
        "corridor": "Corridor",
        "corridor_node": "Door / corridor node",
        "amr": "AMR",
    }

    def __init__(self, parent, config, store_data, topology_selection=None):
        super().__init__(parent)
        self.setWindowTitle("Scenario testing")
        self.resize(1180, 720)
        self.setMinimumSize(780, 520)
        self.result = None
        self.config = dict(config or {})
        self.scenarios = [dict(x) for x in self.config.get("scenarios", []) or []]
        corridors = store_data.get("corridors", {})
        self.resource_options = {
            "lift": sorted(
                str(x.get("id", ""))
                for x in store_data.get("lifts", [])
                if str(x.get("id", ""))
            ),
            "amr": sorted(
                str(x.get("id", ""))
                for x in store_data.get("amrs", [])
                if str(x.get("id", ""))
            ),
            "corridor": sorted(
                f"{x.get('from', '')} -> {x.get('to', '')}"
                for x in corridors.get("edges", [])
                if x.get("from") and x.get("to")
            ),
            "corridor_node": sorted(
                str(x.get("name", ""))
                for x in corridors.get("nodes", [])
                if str(x.get("name", ""))
            ),
        }
        self.topology_selection = dict(topology_selection or {})

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Compare normal operation with controlled failures, closures, reduced availability, "
                "and slower assets. Select a scenario, add one or more timed events, then enable scenario mode."
            )
        )

        options_box = QGroupBox("Run options")
        options_layout = QHBoxLayout(options_box)
        self.enabled_check = QCheckBox("Run the selected scenario")
        self.enabled_check.setChecked(bool(self.config.get("enabled", False)))
        self.enabled_check.setToolTip(
            "When cleared, the simulator uses normal operation even if a scenario is selected."
        )
        self.enhanced_check = QCheckBox("Include detailed payload-transition logging")
        self.enhanced_check.setChecked(bool(self.config.get("enhanced_logging", False)))
        self.enhanced_check.setToolTip(
            "Scenario runs normally use compact CSV logging. Enable this only when checking payload state changes."
        )
        options_layout.addWidget(self.enabled_check)
        options_layout.addWidget(self.enhanced_check)
        options_layout.addStretch(1)
        layout.addWidget(options_box)

        scenario_box = QGroupBox("Scenario")
        scenario_layout = QGridLayout(scenario_box)
        scenario_layout.addWidget(QLabel("Selected scenario"), 0, 0)
        self.scenario_combo = QComboBox()
        scenario_layout.addWidget(self.scenario_combo, 0, 1)
        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.new_scenario)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate_scenario)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self.rename_scenario)
        self.delete_scenario_button = QPushButton("Delete")
        self.delete_scenario_button.clicked.connect(self.delete_scenario)
        scenario_layout.addWidget(self.new_button, 0, 2)
        scenario_layout.addWidget(self.duplicate_button, 0, 3)
        scenario_layout.addWidget(self.rename_button, 0, 4)
        scenario_layout.addWidget(self.delete_scenario_button, 0, 5)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setMaximumHeight(85)
        self.description_edit.setPlaceholderText(
            "State the purpose and assumptions, for example one passenger lift unavailable during visiting hours."
        )
        scenario_layout.addWidget(QLabel("Description"), 1, 0, Qt.AlignTop)
        scenario_layout.addWidget(self.description_edit, 1, 1, 1, 5)
        layout.addWidget(scenario_box)

        events_box = QGroupBox("Events in the selected scenario")
        events_layout = QVBoxLayout(events_box)
        self.event_summary = QLabel()
        events_layout.addWidget(self.event_summary)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Asset type",
                "Affected assets",
                "Daily window",
                "Availability",
                "Speed",
                "Days",
                "Effect",
                "Notes",
            ]
        )
        _configure_data_table(self.table)
        self.table.doubleClicked.connect(self.edit_event)
        events_layout.addWidget(self.table, 1)

        event_row = QHBoxLayout()
        self.add_event_button = QPushButton("Add event")
        self.add_event_button.clicked.connect(self.add_event)
        self.add_topology_button = QPushButton("Add current topology selection")
        self.add_topology_button.clicked.connect(self.add_topology_event)
        self.add_topology_button.setEnabled(
            bool(self.topology_selection.get("corridor"))
            or bool(self.topology_selection.get("corridor_node"))
        )
        self.add_topology_button.setToolTip(
            "Creates an event using the corridor edges or door nodes currently selected in the topology view."
        )
        self.edit_event_button = QPushButton("Edit")
        self.edit_event_button.clicked.connect(self.edit_event)
        self.delete_event_button = QPushButton("Delete")
        self.delete_event_button.clicked.connect(self.delete_event)
        for button in [
            self.add_event_button,
            self.add_topology_button,
            self.edit_event_button,
            self.delete_event_button,
        ]:
            event_row.addWidget(button)
        event_row.addStretch(1)
        events_layout.addLayout(event_row)
        layout.addWidget(events_box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.scenario_combo.currentIndexChanged.connect(self._scenario_changed)
        self.enabled_check.toggled.connect(self._update_enabled_state)
        self._refresh_scenarios()
        _polish_dialog(self)

    def _refresh_scenarios(self):
        current = str(self.config.get("active_scenario", "Normal operation"))
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.clear()
        self.scenario_combo.addItem("Normal operation")
        for scenario in self.scenarios:
            self.scenario_combo.addItem(str(scenario.get("name", "Scenario")))
        idx = self.scenario_combo.findText(current)
        self.scenario_combo.setCurrentIndex(max(0, idx))
        self.scenario_combo.blockSignals(False)
        self.load_scenario()

    def _scenario_changed(self):
        self._save_current_description()
        self.config["active_scenario"] = self.scenario_combo.currentText().strip() or "Normal operation"
        self.load_scenario()

    def _save_current_description(self):
        idx = self.scenario_combo.currentIndex() - 1
        if 0 <= idx < len(self.scenarios):
            self.scenarios[idx]["description"] = self.description_edit.toPlainText().strip()

    def current_scenario(self):
        idx = self.scenario_combo.currentIndex() - 1
        return self.scenarios[idx] if 0 <= idx < len(self.scenarios) else None

    @staticmethod
    def _effect_text(event):
        availability = max(0.0, min(100.0, float(event.get("availability_percent", 100.0) or 0.0)))
        speed = max(0.0, min(100.0, float(event.get("speed_factor", 1.0) or 0.0) * 100.0))
        if availability <= 0:
            return "Outage"
        if speed <= 0:
            return "Unable to move"
        if availability < 100 and speed < 100:
            return "Limited and slowed"
        if availability < 100:
            return "Limited capacity"
        if speed < 100:
            return "Slowed"
        return "No reduction"

    def load_scenario(self):
        scenario = self.current_scenario()
        normal = scenario is None
        self.description_edit.setReadOnly(normal)
        self.description_edit.setPlainText(
            str(scenario.get("description", ""))
            if scenario
            else (
                "Normal operation assumes configured lifts, corridors and AMRs are available. "
                "Lift health, routine failures, charging constraints and normal people profiles still apply."
            )
        )
        self.table.setRowCount(0)
        events = scenario.get("events", []) if scenario else []
        for event in events:
            row = self.table.rowCount()
            self.table.insertRow(row)
            resources = list(event.get("resource_ids", []) or [])
            if not resources and event.get("resource_id"):
                resources = [event.get("resource_id")]
            resource_text = "; ".join(str(x) for x in resources)
            if len(resource_text) > 90:
                resource_text = f"{len(resources)} assets: " + resource_text[:75] + "…"
            start = str(event.get("start_time", "00:00") or "00:00")
            end = str(event.get("end_time", "00:00") or "00:00")
            values = [
                self.RESOURCE_LABELS.get(str(event.get("resource_type", "")), str(event.get("resource_type", ""))),
                resource_text,
                "All day" if start == end else f"{start}–{end}",
                f"{float(event.get('availability_percent', 0) or 0):g}%",
                f"{float(event.get('speed_factor', 1) or 0) * 100:g}%",
                ", ".join(str(day).title() for day in (event.get("days_active", []) or [])),
                self._effect_text(event),
                event.get("notes", ""),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.event_summary.setText(
            "Normal operation has no scenario events."
            if normal
            else f"{len(events)} event(s) configured. Double-click a row to edit it."
        )
        self._update_enabled_state()

    def _update_enabled_state(self):
        scenario = self.current_scenario()
        editable = scenario is not None
        for widget in [
            self.description_edit,
            self.duplicate_button,
            self.rename_button,
            self.delete_scenario_button,
            self.add_event_button,
            self.edit_event_button,
            self.delete_event_button,
        ]:
            widget.setEnabled(editable)
        self.add_topology_button.setEnabled(
            editable
            and (
                bool(self.topology_selection.get("corridor"))
                or bool(self.topology_selection.get("corridor_node"))
            )
        )
        self.enabled_check.setEnabled(editable)
        if not editable:
            self.enabled_check.setChecked(False)

    def _unique_name(self, requested):
        names = {str(item.get("name", "")).strip().lower() for item in self.scenarios}
        base = str(requested or "Scenario").strip() or "Scenario"
        candidate = base
        index = 2
        while candidate.lower() in names:
            candidate = f"{base} {index}"
            index += 1
        return candidate

    def new_scenario(self):
        self._save_current_description()
        name, ok = QInputDialog.getText(
            self,
            "New scenario",
            "Scenario name:",
            text=self._unique_name("New scenario"),
        )
        if ok and name.strip():
            unique = self._unique_name(name.strip())
            self.scenarios.append({"name": unique, "description": "", "events": []})
            self.config["active_scenario"] = unique
            self._refresh_scenarios()

    def duplicate_scenario(self):
        scenario = self.current_scenario()
        if not scenario:
            return
        import copy

        clone = copy.deepcopy(scenario)
        clone["name"] = self._unique_name(f"{scenario.get('name', 'Scenario')} copy")
        self.scenarios.append(clone)
        self.config["active_scenario"] = clone["name"]
        self._refresh_scenarios()

    def rename_scenario(self):
        scenario = self.current_scenario()
        if not scenario:
            return
        original = str(scenario.get("name", ""))
        name, ok = QInputDialog.getText(
            self,
            "Rename scenario",
            "Scenario name:",
            text=original,
        )
        if ok and name.strip():
            requested = name.strip()
            other_names = {
                str(item.get("name", "")).strip().lower()
                for item in self.scenarios
                if item is not scenario
            }
            if requested.lower() in other_names:
                QMessageBox.warning(self, "Scenario name", "Another scenario already uses that name.")
                return
            scenario["name"] = requested
            self.config["active_scenario"] = requested
            self._refresh_scenarios()

    def delete_scenario(self):
        idx = self.scenario_combo.currentIndex() - 1
        if idx < 0:
            return
        name = str(self.scenarios[idx].get("name", "this scenario"))
        answer = QMessageBox.question(
            self,
            "Delete scenario",
            f"Delete {name} and all of its events?",
        )
        if answer == QMessageBox.Yes:
            del self.scenarios[idx]
            self.config["active_scenario"] = "Normal operation"
            self._refresh_scenarios()

    def selected_event(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _ensure_scenario(self):
        scenario = self.current_scenario()
        if scenario is None:
            QMessageBox.information(
                self,
                "Scenario testing",
                "Create or select a scenario before adding events.",
            )
        return scenario

    def add_event(self):
        scenario = self._ensure_scenario()
        if not scenario:
            return
        dialog = ScenarioEventDialog(self, self.resource_options)
        if dialog.exec() == QDialog.Accepted and dialog.result:
            scenario.setdefault("events", []).append(dialog.result)
            self.load_scenario()
            self.table.selectRow(len(scenario["events"]) - 1)

    def add_topology_event(self):
        scenario = self._ensure_scenario()
        if not scenario:
            return
        available_types = []
        if self.topology_selection.get("corridor"):
            available_types.append(("Corridor edges", "corridor"))
        if self.topology_selection.get("corridor_node"):
            available_types.append(("Corridor nodes / doors", "corridor_node"))
        if not available_types:
            QMessageBox.information(
                self,
                "Topology selection",
                "Select one or more corridor edges or corridor nodes in the topology view first.",
            )
            return
        resource_type = available_types[0][1]
        if len(available_types) > 1:
            labels = [label for label, _value in available_types]
            choice, ok = QInputDialog.getItem(
                self,
                "Topology selection",
                "Create the event for:",
                labels,
                0,
                False,
            )
            if not ok:
                return
            resource_type = next(value for label, value in available_types if label == choice)
        seed = {
            "resource_type": resource_type,
            "resource_ids": list(self.topology_selection.get(resource_type, []) or []),
            "availability_percent": 0.0,
            "speed_factor": 1.0,
        }
        dialog = ScenarioEventDialog(self, self.resource_options, seed)
        if dialog.exec() == QDialog.Accepted and dialog.result:
            scenario.setdefault("events", []).append(dialog.result)
            self.load_scenario()
            self.table.selectRow(len(scenario["events"]) - 1)

    def edit_event(self, *_args):
        scenario = self.current_scenario()
        index = self.selected_event()
        if not scenario or index < 0:
            QMessageBox.information(self, "Scenario event", "Select an event to edit.")
            return
        dialog = ScenarioEventDialog(
            self, self.resource_options, scenario.setdefault("events", [])[index]
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            scenario["events"][index] = dialog.result
            self.load_scenario()
            self.table.selectRow(index)

    def delete_event(self):
        scenario = self.current_scenario()
        index = self.selected_event()
        if not scenario or index < 0:
            QMessageBox.information(self, "Scenario event", "Select an event to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete scenario event",
            "Delete the selected event?",
        )
        if answer == QMessageBox.Yes:
            del scenario.setdefault("events", [])[index]
            self.load_scenario()

    def accept(self):
        self._save_current_description()
        active = self.scenario_combo.currentText().strip() or "Normal operation"
        self.result = {
            "enabled": self.enabled_check.isChecked() and active != "Normal operation",
            "active_scenario": active,
            "enhanced_logging": self.enhanced_check.isChecked(),
            "scenarios": self.scenarios,
        }
        super().accept()


class CorridorSettingsDialog(QDialog):
    PEOPLE_LABELS = {
        "none": "None / unrestricted",
        "staff": "Staff",
        "public": "Public",
        "both": "Mixed staff and public",
    }

    def __init__(
        self,
        parent,
        edges,
        nodes,
        default_width=2.4,
        default_door_width=0.9,
        people_profiles=None,
        initially_selected_edges=None,
        initially_selected_nodes=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Corridors, doors and people use")
        self.resize(1220, 720)
        self.setMinimumSize(800, 520)
        self.edges = [dict(x) for x in edges or []]
        self.nodes = [dict(x) for x in nodes or []]
        self.default_width = float(default_width)
        self.default_door_width = float(default_door_width)
        self.people_profiles = sorted(set(str(x) for x in people_profiles or [] if str(x)))
        self.initially_selected_edges = set(initially_selected_edges or [])
        self.initially_selected_nodes = set(initially_selected_nodes or [])
        self.selected_profile_ids = []
        self.profile_change_requested = False
        self.result = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Select one or more rows, choose only the values that need changing, then apply them. "
                "The effective width is the narrowest of the corridor and any door opening at either endpoint."
            )
        )
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self._build_corridor_tab(tabs)
        self._build_door_tab(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_edge_table()
        self._refresh_node_table()
        self._select_initial_rows()
        self._update_edge_selection_summary()
        self._update_node_selection_summary()
        _polish_dialog(self)

    @staticmethod
    def edge_label(edge):
        return f"{edge.get('from', '')} -> {edge.get('to', '')}"

    def _node_map(self):
        return {str(node.get("name", "")): node for node in self.nodes}

    def effective_width(self, edge):
        width = max(0.1, float(edge.get("width_m", self.default_width) or self.default_width))
        node_map = self._node_map()
        for endpoint in (edge.get("from"), edge.get("to")):
            node = node_map.get(str(endpoint))
            if node and bool(node.get("has_door", False)):
                width = min(
                    width,
                    max(
                        0.1,
                        float(
                            node.get("door_clear_width_m", self.default_door_width)
                            or self.default_door_width
                        ),
                    ),
                )
        return width

    def _build_corridor_tab(self, tabs):
        page = QWidget()
        tabs.addTab(page, "Corridors")
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        self.edge_filter = QLineEdit()
        self.edge_filter.setPlaceholderText("Filter by node, people use or profile…")
        self.edge_filter.textChanged.connect(self._filter_edges)
        select_button = QPushButton("Select corridor assets…")
        select_button.clicked.connect(self.select_edge_assets)
        top.addWidget(QLabel("Find"))
        top.addWidget(self.edge_filter, 1)
        top.addWidget(select_button)
        layout.addLayout(top)

        self.edge_selection_summary = QLabel()
        layout.addWidget(self.edge_selection_summary)
        self.edge_table = QTableWidget(0, 7)
        self.edge_table.setHorizontalHeaderLabels(
            [
                "From",
                "To",
                "Traffic",
                "Corridor width",
                "Effective width",
                "People use",
                "People profiles",
            ]
        )
        _configure_data_table(self.edge_table, extended=True)
        self.edge_table.selectionModel().selectionChanged.connect(
            self._update_edge_selection_summary
        )
        layout.addWidget(self.edge_table, 1)

        editor = QGroupBox("Apply settings to selected corridors")
        controls = QGridLayout(editor)
        self.bulk_bidirectional = QComboBox()
        self.bulk_bidirectional.addItem("Leave unchanged", "")
        self.bulk_bidirectional.addItem("Two-way / bidirectional", True)
        self.bulk_bidirectional.addItem("One-way", False)
        self.bulk_width = QDoubleSpinBox()
        self.bulk_width.setRange(0.0, 100.0)
        self.bulk_width.setDecimals(3)
        self.bulk_width.setSpecialValueText("Leave unchanged")
        self.bulk_width.setSuffix(" m")
        self.bulk_width.setToolTip("Nominal clear corridor width before door restrictions.")
        self.bulk_people = QComboBox()
        self.bulk_people.addItem("Leave unchanged", "")
        for value in ["none", "staff", "public", "both"]:
            self.bulk_people.addItem(self.PEOPLE_LABELS[value], value)

        profile_widget = QWidget()
        profile_layout = QHBoxLayout(profile_widget)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        self.profile_summary = QLabel("Leave unchanged")
        self.profile_summary.setWordWrap(True)
        choose_profiles = QPushButton("Choose…")
        choose_profiles.clicked.connect(self.select_usage_profiles)
        clear_profiles = QPushButton("Clear profiles")
        clear_profiles.clicked.connect(self.clear_usage_profiles)
        profile_layout.addWidget(self.profile_summary, 1)
        profile_layout.addWidget(choose_profiles)
        profile_layout.addWidget(clear_profiles)

        apply_button = QPushButton("Apply to selected corridors")
        apply_button.clicked.connect(self.apply_edge_bulk)
        apply_button.setDefault(False)
        controls.addWidget(QLabel("Traffic direction"), 0, 0)
        controls.addWidget(self.bulk_bidirectional, 0, 1)
        controls.addWidget(QLabel("Nominal clear width"), 0, 2)
        controls.addWidget(self.bulk_width, 0, 3)
        controls.addWidget(QLabel("People classification"), 1, 0)
        controls.addWidget(self.bulk_people, 1, 1)
        controls.addWidget(QLabel("Assigned people profiles"), 1, 2)
        controls.addWidget(profile_widget, 1, 3)
        controls.addWidget(apply_button, 0, 4, 2, 1)
        controls.setColumnStretch(3, 1)
        layout.addWidget(editor)

        layout.addWidget(
            _dialog_intro(
                "A two-way corridor can form two AMR lanes only when the AMR and its carried payload "
                "fit within one half of the effective width. People profiles add timed pedestrian use."
            )
        )

    def _build_door_tab(self, tabs):
        page = QWidget()
        tabs.addTab(page, "Door openings")
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.node_filter = QLineEdit()
        self.node_filter.setPlaceholderText("Filter by node name or floor…")
        self.node_filter.textChanged.connect(self._filter_nodes)
        select_button = QPushButton("Select corridor nodes…")
        select_button.clicked.connect(self.select_node_assets)
        top.addWidget(QLabel("Find"))
        top.addWidget(self.node_filter, 1)
        top.addWidget(select_button)
        layout.addLayout(top)

        self.node_selection_summary = QLabel()
        layout.addWidget(self.node_selection_summary)
        self.node_table = QTableWidget(0, 4)
        self.node_table.setHorizontalHeaderLabels(
            ["Corridor node", "Floor", "Door opening", "Clear opening width"]
        )
        _configure_data_table(self.node_table, extended=True)
        self.node_table.selectionModel().selectionChanged.connect(
            self._update_node_selection_summary
        )
        layout.addWidget(self.node_table, 1)

        editor = QGroupBox("Apply settings to selected corridor nodes")
        controls = QGridLayout(editor)
        self.bulk_door = QComboBox()
        self.bulk_door.addItem("Leave unchanged", "")
        self.bulk_door.addItem("This node is a door opening", True)
        self.bulk_door.addItem("This node is not a door", False)
        self.bulk_door_width = QDoubleSpinBox()
        self.bulk_door_width.setRange(0.0, 20.0)
        self.bulk_door_width.setDecimals(3)
        self.bulk_door_width.setSpecialValueText("Leave unchanged")
        self.bulk_door_width.setSuffix(" m")
        self.bulk_door_width.setToolTip(
            "Measure the narrowest clear opening available to the AMR, not the nominal door-set size."
        )
        apply_button = QPushButton("Apply to selected nodes")
        apply_button.clicked.connect(self.apply_node_bulk)
        controls.addWidget(QLabel("Door classification"), 0, 0)
        controls.addWidget(self.bulk_door, 0, 1)
        controls.addWidget(QLabel("Clear opening width"), 0, 2)
        controls.addWidget(self.bulk_door_width, 0, 3)
        controls.addWidget(apply_button, 0, 4)
        controls.setColumnStretch(1, 1)
        layout.addWidget(editor)
        layout.addWidget(
            _dialog_intro(
                "Mark the graph node at the restrictive opening. Every corridor connected to that node "
                "uses the door width when it is narrower than the corridor itself."
            )
        )

    def _refresh_edge_table(self):
        self.edge_table.setRowCount(0)
        for edge in self.edges:
            row = self.edge_table.rowCount()
            self.edge_table.insertRow(row)
            profiles = ", ".join(edge.get("people_profile_ids", []) or [])
            configured = max(0.1, float(edge.get("width_m", self.default_width) or self.default_width))
            effective = self.effective_width(edge)
            values = [
                edge.get("from", ""),
                edge.get("to", ""),
                "Two-way" if edge.get("bidirectional", True) else "One-way",
                f"{configured:.3f} m",
                f"{effective:.3f} m",
                self.PEOPLE_LABELS.get(str(edge.get("people_area_type", "none")), "None / unrestricted"),
                profiles or "None",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 4 and effective < configured - 1e-9:
                    item.setToolTip("Restricted by a door opening at one of the corridor endpoints.")
                self.edge_table.setItem(row, col, item)
        self._filter_edges()

    def _refresh_node_table(self):
        self.node_table.setRowCount(0)
        for node in self.nodes:
            row = self.node_table.rowCount()
            self.node_table.insertRow(row)
            values = [
                node.get("name", ""),
                node.get("floor", ""),
                "Door" if node.get("has_door", False) else "No door",
                f"{float(node.get('door_clear_width_m', self.default_door_width) or self.default_door_width):.3f} m",
            ]
            for col, value in enumerate(values):
                self.node_table.setItem(row, col, QTableWidgetItem(str(value)))
        self._filter_nodes()

    def _filter_edges(self):
        if not hasattr(self, "edge_table"):
            return
        needle = self.edge_filter.text().strip().lower() if hasattr(self, "edge_filter") else ""
        for row, edge in enumerate(self.edges):
            text = " ".join(
                [
                    self.edge_label(edge),
                    str(edge.get("people_area_type", "")),
                    " ".join(edge.get("people_profile_ids", []) or []),
                ]
            ).lower()
            self.edge_table.setRowHidden(row, bool(needle and needle not in text))

    def _filter_nodes(self):
        if not hasattr(self, "node_table"):
            return
        needle = self.node_filter.text().strip().lower() if hasattr(self, "node_filter") else ""
        for row, node in enumerate(self.nodes):
            text = f"{node.get('name', '')} {node.get('floor', '')}".lower()
            self.node_table.setRowHidden(row, bool(needle and needle not in text))

    @staticmethod
    def _set_selected_rows(table, rows):
        table.clearSelection()
        for row in rows:
            if table.isRowHidden(row):
                continue
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item is not None:
                    item.setSelected(True)

    def _select_initial_rows(self):
        edge_rows = [
            row
            for row, edge in enumerate(self.edges)
            if self.edge_label(edge) in self.initially_selected_edges
        ]
        node_rows = [
            row
            for row, node in enumerate(self.nodes)
            if str(node.get("name", "")) in self.initially_selected_nodes
        ]
        self._set_selected_rows(self.edge_table, edge_rows)
        self._set_selected_rows(self.node_table, node_rows)

    def selected_edge_rows(self):
        return sorted({index.row() for index in self.edge_table.selectionModel().selectedRows()})

    def selected_node_rows(self):
        return sorted({index.row() for index in self.node_table.selectionModel().selectedRows()})

    def _update_edge_selection_summary(self, *_args):
        count = len(self.selected_edge_rows())
        self.edge_selection_summary.setText(
            f"{count} corridor(s) selected."
            if count
            else "Select corridor rows to edit them together. Ctrl/Shift-click adds to the selection."
        )

    def _update_node_selection_summary(self, *_args):
        count = len(self.selected_node_rows())
        self.node_selection_summary.setText(
            f"{count} corridor node(s) selected."
            if count
            else "Select corridor nodes to mark or remove door restrictions."
        )

    def select_edge_assets(self):
        options = [self.edge_label(edge) for edge in self.edges]
        selected = [self.edge_label(self.edges[row]) for row in self.selected_edge_rows()]
        dialog = MultiSelectPicker(self, "Select corridor assets", options, selected=selected)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            wanted = set(dialog.result)
            self.edge_filter.clear()
            self._set_selected_rows(
                self.edge_table,
                [row for row, edge in enumerate(self.edges) if self.edge_label(edge) in wanted],
            )

    def select_node_assets(self):
        options = [str(node.get("name", "")) for node in self.nodes]
        selected = [str(self.nodes[row].get("name", "")) for row in self.selected_node_rows()]
        dialog = MultiSelectPicker(
            self,
            "Select corridor nodes and door openings",
            options,
            selected=selected,
            group_resolver=lambda value: f"Floor {self._node_map().get(value, {}).get('floor', '')}",
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            wanted = set(dialog.result)
            self.node_filter.clear()
            self._set_selected_rows(
                self.node_table,
                [
                    row
                    for row, node in enumerate(self.nodes)
                    if str(node.get("name", "")) in wanted
                ],
            )

    def select_usage_profiles(self):
        dialog = MultiSelectPicker(
            self,
            "Select people movement profiles",
            self.people_profiles,
            selected=self.selected_profile_ids,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.selected_profile_ids = list(dialog.result)
            self.profile_change_requested = True
            self.profile_summary.setText(
                ", ".join(self.selected_profile_ids) if self.selected_profile_ids else "Clear all profiles"
            )

    def clear_usage_profiles(self):
        self.selected_profile_ids = []
        self.profile_change_requested = True
        self.profile_summary.setText("Clear all profiles")

    def apply_edge_bulk(self):
        rows = self.selected_edge_rows()
        if not rows:
            QMessageBox.information(self, "Corridors", "Select one or more corridor rows first.")
            return
        changed = False
        bidirectional_data = self.bulk_bidirectional.currentData()
        width = self.bulk_width.value() if self.bulk_width.value() > 0.0 else None
        people_value = str(self.bulk_people.currentData() or "")
        for row in rows:
            edge = self.edges[row]
            if bidirectional_data != "":
                edge["bidirectional"] = bool(bidirectional_data)
                changed = True
            if width is not None:
                edge["width_m"] = float(width)
                changed = True
            if people_value:
                edge["people_area_type"] = people_value
                changed = True
            if self.profile_change_requested:
                edge["people_profile_ids"] = list(self.selected_profile_ids)
                changed = True
        if not changed:
            QMessageBox.information(
                self,
                "Corridors",
                "Choose at least one setting to apply. Values showing 'Leave unchanged' are ignored.",
            )
            return
        self._refresh_edge_table()
        self._set_selected_rows(self.edge_table, rows)
        self.profile_change_requested = False
        self.profile_summary.setText("Leave unchanged")
        self.bulk_bidirectional.setCurrentIndex(0)
        self.bulk_width.setValue(0.0)
        self.bulk_people.setCurrentIndex(0)

    def apply_node_bulk(self):
        rows = self.selected_node_rows()
        if not rows:
            QMessageBox.information(self, "Door openings", "Select one or more corridor nodes first.")
            return
        changed = False
        door_data = self.bulk_door.currentData()
        width = self.bulk_door_width.value() if self.bulk_door_width.value() > 0.0 else None
        for row in rows:
            node = self.nodes[row]
            if door_data != "":
                node["has_door"] = bool(door_data)
                changed = True
            if width is not None:
                node["door_clear_width_m"] = float(width)
                changed = True
        if not changed:
            QMessageBox.information(
                self,
                "Door openings",
                "Choose a door classification or enter a clear opening width to apply.",
            )
            return
        self._refresh_node_table()
        self._refresh_edge_table()
        self._set_selected_rows(self.node_table, rows)
        self.bulk_door.setCurrentIndex(0)
        self.bulk_door_width.setValue(0.0)

    def accept(self):
        out_edges = []
        for original in self.edges:
            edge = dict(original)
            edge["bidirectional"] = bool(edge.get("bidirectional", True))
            edge["width_m"] = max(
                0.1, float(edge.get("width_m", self.default_width) or self.default_width)
            )
            area = str(edge.get("people_area_type", "none") or "none").strip().lower()
            if area == "mixed":
                area = "both"
            edge["people_area_type"] = area if area in {"none", "staff", "public", "both"} else "none"
            edge["people_profile_ids"] = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in (edge.get("people_profile_ids", []) or [])
                    if str(value).strip()
                )
            )
            out_edges.append(edge)
        out_nodes = []
        for original in self.nodes:
            node = dict(original)
            node["has_door"] = bool(node.get("has_door", False))
            node["door_clear_width_m"] = max(
                0.1,
                float(
                    node.get("door_clear_width_m", self.default_door_width)
                    or self.default_door_width
                ),
            )
            out_nodes.append(node)
        self.result = {"edges": out_edges, "nodes": out_nodes}
        super().accept()


class SimulationSettingsDialog(QDialog):
    def __init__(self, parent, simulation=None):
        super().__init__(parent)
        self.setWindowTitle("Simulation settings")
        self.resize(780, 640)
        self.setMinimumSize(660, 500)

        self.simulation = dict(simulation or {})
        simulation = self._normalise_simulation(self.simulation)
        self.result = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            _dialog_intro(
                "Set the simulated period and initial conditions. Performance controls normally "
                "do not change the result; they limit how much scheduling work is done in one UI cycle."
            )
        )
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        period_page = QWidget()
        tabs.addTab(period_page, "Run period")
        period_layout = QVBoxLayout(period_page)
        period_box = QGroupBox("Simulation dates and speed")
        period_form = QFormLayout(period_box)
        self.start_datetime_edit = QDateTimeEdit()
        self.start_datetime_edit.setCalendarPopup(True)
        self.start_datetime_edit.setDisplayFormat("dd MMM yyyy HH:mm:ss")
        self.start_datetime_edit.setDateTime(
            self._datetime_from_text(simulation.get("start_datetime"), "2026-01-05T06:00:00")
        )
        self.use_end_datetime_check = QCheckBox("Stop at a fixed end date and time")
        self.use_end_datetime_check.setChecked(bool(str(simulation.get("end_datetime", "") or "").strip()))
        self.end_datetime_edit = QDateTimeEdit()
        self.end_datetime_edit.setCalendarPopup(True)
        self.end_datetime_edit.setDisplayFormat("dd MMM yyyy HH:mm:ss")
        self.end_datetime_edit.setDateTime(
            self._datetime_from_text(
                simulation.get("end_datetime") or "2026-01-06T06:00:00",
                "2026-01-06T06:00:00",
            )
        )
        self.end_datetime_edit.setEnabled(self.use_end_datetime_check.isChecked())
        self.use_end_datetime_check.toggled.connect(self.end_datetime_edit.setEnabled)

        self.tick_rate_spin = QDoubleSpinBox()
        self.tick_rate_spin.setRange(0.001, 1000000000.0)
        self.tick_rate_spin.setDecimals(3)
        self.tick_rate_spin.setValue(max(0.001, float(simulation.get("tick_rate", 1000) or 1000)))
        self.tick_rate_spin.setSuffix("×")
        self.tick_rate_spin.setToolTip(
            "Simulation speed multiplier. This controls playback/processing speed, not AMR travel speed."
        )
        self.generated_stagger_spin = QDoubleSpinBox()
        self.generated_stagger_spin.setRange(0.0, 3600.0)
        self.generated_stagger_spin.setDecimals(3)
        self.generated_stagger_spin.setValue(
            max(0.0, float(simulation.get("generated_task_release_stagger_sec", 0.25) or 0.0))
        )
        self.generated_stagger_spin.setSuffix(" s")
        self.generated_stagger_spin.setToolTip(
            "Adds a small spacing between tasks generated for exactly the same instant."
        )
        period_form.addRow("Starts", self.start_datetime_edit)
        period_form.addRow("End condition", self.use_end_datetime_check)
        period_form.addRow("Ends", self.end_datetime_edit)
        period_form.addRow("Simulation rate", self.tick_rate_spin)
        period_form.addRow("Same-time task release spacing", self.generated_stagger_spin)
        period_layout.addWidget(period_box)
        period_layout.addWidget(
            _dialog_intro(
                "Leave the fixed end option clear when another configured stopping condition controls the run. "
                "The end date must be later than the start date."
            )
        )
        period_layout.addStretch(1)

        performance_page = QWidget()
        tabs.addTab(performance_page, "Performance")
        performance_layout = QVBoxLayout(performance_page)
        route_box = QGroupBox("Route preparation")
        route_form = QFormLayout(route_box)
        self.precompute_routes_check = QCheckBox("Prepare commonly used routes before the run")
        self.precompute_routes_check.setChecked(bool(simulation.get("precompute_static_routes", True)))
        self.precompute_routes_check.setToolTip(
            "Reduces route-search pauses during the simulation at the cost of some startup work."
        )
        self.route_precompute_max_pairs_spin = QSpinBox()
        self.route_precompute_max_pairs_spin.setRange(0, 1000000000)
        self.route_precompute_max_pairs_spin.setValue(
            max(0, int(float(simulation.get("route_precompute_max_pairs", 100000) or 0)))
        )
        self.route_precompute_max_pairs_spin.setSpecialValueText("No precompute limit")
        route_form.addRow("Route precomputation", self.precompute_routes_check)
        route_form.addRow("Maximum route pairs", self.route_precompute_max_pairs_spin)
        performance_layout.addWidget(route_box)

        scheduling_box = QGroupBox("Scheduling workload limits")
        scheduling_form = QFormLayout(scheduling_box)
        self.max_multi_stop_candidate_tasks_spin = QSpinBox()
        self.max_multi_stop_candidate_tasks_spin.setRange(1, 1000000)
        self.max_multi_stop_candidate_tasks_spin.setValue(
            max(1, int(float(simulation.get("max_multi_stop_candidate_tasks", 8) or 8)))
        )
        self.max_single_candidate_tasks_spin = QSpinBox()
        self.max_single_candidate_tasks_spin.setRange(1, 1000000)
        self.max_single_candidate_tasks_spin.setValue(
            max(1, int(float(simulation.get("max_single_candidate_tasks", 8) or 8)))
        )
        self.max_assignments_per_tick_spin = QSpinBox()
        self.max_assignments_per_tick_spin.setRange(1, 1000000)
        self.max_assignments_per_tick_spin.setValue(
            max(1, int(float(simulation.get("max_assignments_per_tick", 25) or 25)))
        )
        self.assignment_continue_delay_spin = QDoubleSpinBox()
        self.assignment_continue_delay_spin.setRange(0.0, 60.0)
        self.assignment_continue_delay_spin.setDecimals(6)
        self.assignment_continue_delay_spin.setValue(
            max(0.0, float(simulation.get("assignment_continue_delay_sec", 0.001) or 0.0))
        )
        self.assignment_continue_delay_spin.setSuffix(" s")
        scheduling_form.addRow("Multi-stop tasks considered together", self.max_multi_stop_candidate_tasks_spin)
        scheduling_form.addRow("Single tasks considered together", self.max_single_candidate_tasks_spin)
        scheduling_form.addRow("Assignments processed per cycle", self.max_assignments_per_tick_spin)
        scheduling_form.addRow("Pause before continuing assignments", self.assignment_continue_delay_spin)
        performance_layout.addWidget(scheduling_box)
        performance_layout.addWidget(
            _dialog_intro(
                "The defaults are suitable for most models. Increase these limits only when a large release "
                "of tasks is not being considered quickly enough; lower them if the interface becomes unresponsive."
            )
        )
        performance_layout.addStretch(1)

        initial_page = QWidget()
        tabs.addTab(initial_page, "Initial conditions")
        initial_layout = QVBoxLayout(initial_page)
        inventory_box = QGroupBox("Inventory and container state")
        inventory_layout = QVBoxLayout(inventory_box)
        self.seed_waste_containers_check = QCheckBox(
            "Place waste containers at department pickup locations when the simulation starts"
        )
        self.seed_waste_containers_check.setChecked(
            bool(simulation.get("seed_waste_stream_containers_at_start", False))
        )
        self.seed_waste_containers_check.setToolTip(
            "Use this when departments are assumed to begin the model with an empty physical waste container available."
        )
        self.disable_inventory_spaces_check = QCheckBox(
            "Ignore finite inventory-space capacity during the simulation"
        )
        self.disable_inventory_spaces_check.setChecked(
            bool(
                simulation.get(
                    "disable_inventory_spaces",
                    simulation.get("inventory_spaces_disabled", False),
                )
            )
        )
        self.disable_inventory_spaces_check.setToolTip(
            "Location boundaries and spaces remain saved and available to reports, but slot occupancy does not block tasks."
        )
        inventory_layout.addWidget(self.seed_waste_containers_check)
        inventory_layout.addWidget(self.disable_inventory_spaces_check)
        initial_layout.addWidget(inventory_box)
        initial_layout.addWidget(
            _dialog_intro(
                "Ignoring inventory capacity is useful for sensitivity tests, but it can hide insufficient storage "
                "or parking provision. Keep it disabled for capacity-planning runs."
            )
        )
        initial_layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    @staticmethod
    def _datetime_from_text(value, fallback):
        text = str(value or fallback).strip()
        parsed = QDateTime.fromString(text, Qt.ISODate)
        if not parsed.isValid():
            parsed = QDateTime.fromString(str(fallback), Qt.ISODate)
        return parsed

    def _normalise_simulation(self, simulation):
        result = dict(simulation or {})
        result.setdefault("start_datetime", "2026-01-05T06:00:00")
        result.setdefault("end_datetime", "2026-01-06T06:00:00")
        result.setdefault("tick_rate", 1000)
        result.setdefault("generated_task_release_stagger_sec", 0.25)
        result.setdefault("precompute_static_routes", True)
        result.setdefault("route_precompute_max_pairs", 100000)
        result.setdefault("max_multi_stop_candidate_tasks", 8)
        result.setdefault("max_single_candidate_tasks", 8)
        result.setdefault("max_assignments_per_tick", 25)
        result.setdefault("assignment_continue_delay_sec", 0.001)
        result.setdefault("seed_waste_stream_containers_at_start", False)
        result.setdefault("disable_inventory_spaces", False)
        return result

    def accept(self):
        start = self.start_datetime_edit.dateTime()
        end = self.end_datetime_edit.dateTime()
        if self.use_end_datetime_check.isChecked() and end <= start:
            QMessageBox.warning(
                self,
                "Check simulation period",
                "The simulation end date and time must be later than the start.",
            )
            return

        result = dict(self.simulation)
        result.update(
            {
                "start_datetime": start.toString(Qt.ISODate),
                "end_datetime": end.toString(Qt.ISODate)
                if self.use_end_datetime_check.isChecked()
                else "",
                "tick_rate": float(self.tick_rate_spin.value()),
                "generated_task_release_stagger_sec": float(self.generated_stagger_spin.value()),
                "precompute_static_routes": self.precompute_routes_check.isChecked(),
                "route_precompute_max_pairs": int(self.route_precompute_max_pairs_spin.value()),
                "max_multi_stop_candidate_tasks": int(self.max_multi_stop_candidate_tasks_spin.value()),
                "max_single_candidate_tasks": int(self.max_single_candidate_tasks_spin.value()),
                "max_assignments_per_tick": int(self.max_assignments_per_tick_spin.value()),
                "assignment_continue_delay_sec": float(self.assignment_continue_delay_spin.value()),
                "seed_waste_stream_containers_at_start": self.seed_waste_containers_check.isChecked(),
                "disable_inventory_spaces": self.disable_inventory_spaces_check.isChecked(),
            }
        )
        self.result = result
        super().accept()


class WasteStreamEditorDialog(QDialog):
    def __init__(self, parent, payload_names, seed=None):
        super().__init__(parent)
        self.setWindowTitle("Waste Stream")
        self.result = None
        self.seed = seed or {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_edit = QLineEdit(self.seed.get("name", "clinical"))
        self.payload_combo = QComboBox()
        self.payload_combo.addItems([""] + list(payload_names))
        self.payload_combo.setCurrentText(self.seed.get("payload", ""))

        self.container_capacity_edit = _double_input(
            self.seed.get("container_capacity_m3", 0.24),
            minimum=0.001,
            maximum=1000.0,
            decimals=3,
            suffix=" m³",
            step=0.01,
            tooltip="Usable internal volume of one waste container.",
        )
        self.full_threshold_edit = _double_input(
            float(self.seed.get("full_threshold_fraction", 0.8) or 0.8) * 100.0,
            minimum=0.1,
            maximum=100.0,
            decimals=1,
            suffix=" %",
            step=1.0,
            tooltip="Container fill level at which the simulator treats it as full.",
        )

        form.addRow("Waste stream name", self.name_edit)
        form.addRow("Container payload", self.payload_combo)
        form.addRow("Container capacity", self.container_capacity_edit)
        form.addRow("Full threshold", self.full_threshold_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def accept(self):
        try:
            name = self.name_edit.text().strip()
            if not name:
                raise ValueError("Waste stream name is required")

            payload = self.payload_combo.currentText().strip()
            if not payload:
                raise ValueError("Container payload is required")

            container_capacity = float(self.container_capacity_edit.value())
            threshold = float(self.full_threshold_edit.value()) / 100.0

            self.result = {
                "name": name,
                "payload": payload,
                "container_capacity_m3": container_capacity,
                "full_threshold_fraction": threshold,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid waste stream", str(exc))


class WasteStreamListDialog(QDialog):
    def __init__(self, parent, payload_names, items, on_save):
        super().__init__(parent)
        self.setWindowTitle("Waste Streams")
        self.resize(760, 420)
        self.payload_names = list(payload_names)
        self.items = [dict(x) for x in items]
        self.on_save = on_save

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            [
                "Name",
                "Container payload",
                "Capacity m3",
                "Full threshold",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 120)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        layout.addLayout(row)

        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        del_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")

        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(del_btn)
        row.addStretch(1)
        row.addWidget(save_btn)

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        del_btn.clicked.connect(self.delete_item)
        save_btn.clicked.connect(self.save_items)

        self._refresh_table()
        _polish_dialog(self)

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(item.get("name", ""))))
            self.table.setItem(row, 1, QTableWidgetItem(str(item.get("payload", ""))))
            self.table.setItem(
                row,
                2,
                QTableWidgetItem(str(item.get("container_capacity_m3", ""))),
            )
            self.table.setItem(
                row,
                3,
                QTableWidgetItem(str(item.get("full_threshold_fraction", ""))),
            )

    def add_item(self):
        dialog = WasteStreamEditorDialog(self, self.payload_names)
        if dialog.exec() == QDialog.Accepted and dialog.result:
            name = dialog.result["name"]
            if any(str(x.get("name", "")).strip() == name for x in self.items):
                QMessageBox.critical(self, "Duplicate", "Waste stream already exists")
                return
            self.items.append(dialog.result)
            self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        dialog = WasteStreamEditorDialog(self, self.payload_names, self.items[row])
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_name = dialog.result["name"]
            for idx, item in enumerate(self.items):
                if idx != row and str(item.get("name", "")).strip() == new_name:
                    QMessageBox.critical(
                        self, "Duplicate", "Waste stream already exists"
                    )
                    return
            self.items[row] = dialog.result
            self._refresh_table()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._refresh_table()

    def save_items(self):
        self.on_save(self.items)
        self.accept()


class MassCollectionEditorDialog(QDialog):
    DAYS = [
        ("mon", "Mon"),
        ("tue", "Tue"),
        ("wed", "Wed"),
        ("thu", "Thu"),
        ("fri", "Fri"),
        ("sat", "Sat"),
        ("sun", "Sun"),
    ]

    def __init__(
        self,
        parent,
        location_names,
        payload_names,
        seed=None,
        default_id="MASS-COLLECTION-1",
    ):
        super().__init__(parent)
        self.setWindowTitle("Mass collection / bin rotation")
        self.resize(760, 620)
        self.result = None
        self.seed = dict(seed or {})
        self.location_names = sorted(location_names)
        self.payload_names = sorted(payload_names)
        self.selected_payloads = list(self.seed.get("payloads", []))

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.id_edit = QLineEdit(str(self.seed.get("id", default_id)))
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(self.seed.get("enabled", True)))

        self.location_combo = QComboBox()
        self.location_combo.addItems([""] + self.location_names)
        self.location_combo.setCurrentText(str(self.seed.get("location", "")))

        payload_row = QHBoxLayout()
        self.payload_summary = QLabel()
        self.payload_summary.setWordWrap(True)
        payload_btn = QPushButton("Select...")
        payload_btn.clicked.connect(self.pick_payloads)
        clear_payload_btn = QPushButton("Clear")
        clear_payload_btn.clicked.connect(self.clear_payloads)
        payload_row.addWidget(self.payload_summary, 1)
        payload_row.addWidget(payload_btn)
        payload_row.addWidget(clear_payload_btn)

        self.scheduled_times = sorted(set(self.seed.get("scheduled_times", [])))
        schedule_widget = QWidget()
        schedule_row = QHBoxLayout(schedule_widget)
        schedule_row.setContentsMargins(0, 0, 0, 0)
        self.scheduled_times_summary = QLabel()
        self.scheduled_times_summary.setWordWrap(True)
        edit_schedule_btn = QPushButton("Edit times...")
        clear_schedule_btn = QPushButton("Clear")
        edit_schedule_btn.clicked.connect(self.edit_scheduled_times)
        clear_schedule_btn.clicked.connect(self.clear_scheduled_times)
        schedule_row.addWidget(self.scheduled_times_summary, 1)
        schedule_row.addWidget(edit_schedule_btn)
        schedule_row.addWidget(clear_schedule_btn)

        self.capacity_fraction_edit = _double_input(
            float(self.seed.get("capacity_trigger_fraction", 0.0) or 0.0) * 100.0,
            minimum=0.0,
            maximum=100.0,
            decimals=1,
            suffix=" %",
            step=1.0,
            tooltip="Optional percentage of configured spaces that triggers a collection. Set to 0% to disable.",
        )
        self.capacity_count_edit = _integer_input(
            self.seed.get("capacity_trigger_count", 0),
            minimum=0,
            maximum=1_000_000,
            suffix=" bin(s)",
            tooltip="Optional fixed number of full bins that triggers a collection. Set to 0 to disable.",
        )
        self.interval_edit = _double_input(
            self.seed.get("capacity_check_interval_minutes", 15.0),
            minimum=0.1,
            maximum=100_000.0,
            decimals=1,
            suffix=" min",
            step=1.0,
            tooltip="How often the simulator checks the capacity trigger.",
        )
        self.replace_check = QCheckBox(
            "Replace collected used/full bins with empty equivalents"
        )
        self.replace_check.setChecked(
            bool(self.seed.get("replace_with_empty_equivalents", True))
        )
        self.notes_edit = QPlainTextEdit(str(self.seed.get("notes", "")))
        self.notes_edit.setFixedHeight(80)

        days_widget = QWidget()
        days_layout = QHBoxLayout(days_widget)
        days_layout.setContentsMargins(0, 0, 0, 0)
        active_days = set(
            self.seed.get(
                "days_active", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            )
        )
        self.day_checks = {}
        for key, label in self.DAYS:
            chk = QCheckBox(label)
            chk.setChecked(key in active_days)
            self.day_checks[key] = chk
            days_layout.addWidget(chk)
        days_layout.addStretch(1)

        form.addRow("ID", self.id_edit)
        form.addRow("Enabled", self.enabled_check)
        form.addRow("Bin store location", self.location_combo)
        form.addRow("Payload types", payload_row)
        form.addRow("Days active", days_widget)
        form.addRow("Scheduled collection times", schedule_widget)
        form.addRow("Capacity trigger", self.capacity_fraction_edit)
        form.addRow("Full-bin trigger", self.capacity_count_edit)
        form.addRow("Capacity check interval", self.interval_edit)
        form.addRow("Exchange", self.replace_check)
        form.addRow("Notes", self.notes_edit)

        help_label = QLabel(
            "At each visit the simulator removes all used/full matching payload instances from the store "
            "and creates the same number of empty replacement instances. Capacity triggers use the number "
            "of configured inventory spaces at the selected location, unless a trigger count is entered."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_payload_summary()
        self.refresh_scheduled_times_summary()
        _polish_dialog(self)

    def edit_scheduled_times(self):
        dialog = ScheduledTimesDialog(self, self.scheduled_times)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.scheduled_times = list(dialog.result)
            self.refresh_scheduled_times_summary()

    def clear_scheduled_times(self):
        self.scheduled_times = []
        self.refresh_scheduled_times_summary()

    def refresh_scheduled_times_summary(self):
        if not self.scheduled_times:
            self.scheduled_times_summary.setText("No fixed times; capacity triggers only")
        elif len(self.scheduled_times) <= 5:
            self.scheduled_times_summary.setText(", ".join(self.scheduled_times))
        else:
            self.scheduled_times_summary.setText(
                f"{len(self.scheduled_times)} times · {self.scheduled_times[0]} to {self.scheduled_times[-1]}"
            )

    def pick_payloads(self):
        picker = MultiSelectPicker(
            self,
            "Select payloads to rotate",
            self.payload_names,
            selected=self.selected_payloads,
            group_resolver=lambda _item: "Payloads",
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_payloads = sorted(picker.result)
            self.refresh_payload_summary()

    def clear_payloads(self):
        self.selected_payloads = []
        self.refresh_payload_summary()

    def refresh_payload_summary(self):
        if not self.selected_payloads:
            self.payload_summary.setText("All payload types")
        elif len(self.selected_payloads) <= 4:
            self.payload_summary.setText(", ".join(self.selected_payloads))
        else:
            self.payload_summary.setText(f"{len(self.selected_payloads)} selected")

    def _parse_times(self):
        return sorted(set(self.scheduled_times))

    def accept(self):
        try:
            collection_id = self.id_edit.text().strip()
            if not collection_id:
                raise ValueError("ID is required")
            location = self.location_combo.currentText().strip()
            if not location:
                raise ValueError("Bin store location is required")
            days = [
                key for key, _label in self.DAYS if self.day_checks[key].isChecked()
            ]
            if not days:
                raise ValueError("Select at least one active day")
            capacity_fraction = float(self.capacity_fraction_edit.value()) / 100.0
            capacity_count = int(self.capacity_count_edit.value())
            interval = float(self.interval_edit.value())
            self.result = {
                "id": collection_id,
                "enabled": self.enabled_check.isChecked(),
                "location": location,
                "payloads": list(self.selected_payloads),
                "days_active": days,
                "scheduled_times": self._parse_times(),
                "capacity_trigger_fraction": capacity_fraction,
                "capacity_trigger_count": capacity_count,
                "capacity_check_interval_minutes": interval,
                "replace_with_empty_equivalents": self.replace_check.isChecked(),
                "notes": self.notes_edit.toPlainText().strip(),
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid mass collection", str(exc))


class MassCollectionListDialog(QDialog):
    def __init__(self, parent, location_names, payload_names, items, on_save):
        super().__init__(parent)
        self.setWindowTitle("Mass collections / bin rotations")
        self.resize(980, 460)
        self.location_names = list(location_names)
        self.payload_names = list(payload_names)
        self.items = [dict(x) for x in items]
        self.on_save = on_save

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Enabled",
                "Location",
                "Payloads",
                "Times",
                "Capacity trigger",
                "Interval min",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        layout.addLayout(row)
        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        del_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")
        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(del_btn)
        row.addStretch(1)
        row.addWidget(save_btn)
        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        del_btn.clicked.connect(self.delete_item)
        save_btn.clicked.connect(self.save_items)
        self._refresh_table()
        _polish_dialog(self)

    def _next_id(self):
        existing = {str(x.get("id", "")) for x in self.items}
        counter = 1
        while True:
            candidate = f"MASS-COLLECTION-{counter}"
            if candidate not in existing:
                return candidate
            counter += 1

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                item.get("id", ""),
                "Yes" if item.get("enabled", True) else "No",
                item.get("location", ""),
                ", ".join(item.get("payloads", [])) or "All",
                ", ".join(item.get("scheduled_times", [])),
                str(
                    item.get("capacity_trigger_count", 0)
                    or item.get("capacity_trigger_fraction", 0.0)
                ),
                str(item.get("capacity_check_interval_minutes", 15.0)),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def add_item(self):
        dialog = MassCollectionEditorDialog(
            self, self.location_names, self.payload_names, default_id=self._next_id()
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if any(
                str(x.get("id", "")).strip() == dialog.result["id"] for x in self.items
            ):
                QMessageBox.critical(
                    self, "Duplicate", "Mass collection ID already exists"
                )
                return
            self.items.append(dialog.result)
            self._refresh_table()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        dialog = MassCollectionEditorDialog(
            self, self.location_names, self.payload_names, seed=self.items[row]
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_id = dialog.result["id"]
            for idx, item in enumerate(self.items):
                if idx != row and str(item.get("id", "")).strip() == new_id:
                    QMessageBox.critical(
                        self, "Duplicate", "Mass collection ID already exists"
                    )
                    return
            self.items[row] = dialog.result
            self._refresh_table()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self._refresh_table()

    def save_items(self):
        self.on_save(self.items)
        self.accept()


class DepartmentWasteStreamSettingsDialog(QDialog):
    MODES = [
        "scheduled",
        "threshold",
        "continuous",
        "sporadic",
        "hybrid",
        "scheduled_threshold",
        "scheduled_sporadic",
    ]

    def __init__(self, parent, waste_stream_names, items=None):
        super().__init__(parent)
        self.setWindowTitle("Department waste stream generation")
        self.resize(900, 520)

        self.waste_stream_names = list(waste_stream_names)
        self.items = [dict(x) for x in (items or [])]
        self.result = None

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "Stream",
                "Mode",
                "Frequency/day",
                "Volume/event m³",
                "Threshold m³",
                "Base daily m³",
                "Scheduled times",
                "Initial container",
                "Shared bin group",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        layout.addLayout(row)

        add_btn = QPushButton("Add stream")
        edit_btn = QPushButton("Edit selected")
        delete_btn = QPushButton("Delete selected")

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        delete_btn.clicked.connect(self.delete_item)

        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(delete_btn)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()
        _polish_dialog(self)

    def refresh(self):
        self.table.setRowCount(0)

        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            values = [
                item.get("name", ""),
                item.get("generation_mode", "threshold"),
                item.get("frequency_per_day", 0.0),
                item.get("volume_per_event_m3", 0.0),
                item.get("threshold_volume_m3", 0.0),
                item.get("base_daily_volume_m3", 0.0),
                ", ".join(item.get("scheduled_times", [])),
                "Yes" if item.get("initial_container_present", True) else "No",
                item.get("shared_container_group", "")
                or ("Shared by pickup" if item.get("shared_container", False) else ""),
            ]

            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def add_item(self):
        dialog = DepartmentWasteStreamItemDialog(
            self,
            self.waste_stream_names,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            name = dialog.result["name"]
            if any(x.get("name") == name for x in self.items):
                QMessageBox.critical(
                    self,
                    "Duplicate",
                    "This waste stream is already assigned to the department.",
                )
                return
            self.items.append(dialog.result)
            self.refresh()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0:
            return

        dialog = DepartmentWasteStreamItemDialog(
            self,
            self.waste_stream_names,
            seed=self.items[row],
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_name = dialog.result["name"]

            for idx, item in enumerate(self.items):
                if idx != row and item.get("name") == new_name:
                    QMessageBox.critical(
                        self,
                        "Duplicate",
                        "This waste stream is already assigned to the department.",
                    )
                    return

            self.items[row] = dialog.result
            self.refresh()
            self.table.selectRow(row)

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.items[row]
        self.refresh()

    def accept(self):
        self.result = [dict(x) for x in self.items]
        super().accept()


class DepartmentWasteStreamItemDialog(QDialog):
    MODES = [
        "scheduled",
        "threshold",
        "continuous",
        "sporadic",
        "hybrid",
        "scheduled_threshold",
        "scheduled_sporadic",
    ]

    def __init__(self, parent, waste_stream_names, seed=None):
        super().__init__(parent)
        self.setWindowTitle("Waste stream generation settings")
        self.resize(520, 420)

        self.seed = seed or {}
        self.result = None
        self.scheduled_times = list(self.seed.get("scheduled_times", []))

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_combo = QComboBox()
        self.name_combo.addItems([""] + list(waste_stream_names))
        self.name_combo.setCurrentText(str(self.seed.get("name", "")))

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(self.MODES)
        self.mode_combo.setCurrentText(
            str(self.seed.get("generation_mode", "threshold"))
        )

        self.frequency_edit = QLineEdit(str(self.seed.get("frequency_per_day", 0.0)))
        self.volume_edit = QLineEdit(str(self.seed.get("volume_per_event_m3", 0.0)))
        self.threshold_edit = QLineEdit(str(self.seed.get("threshold_volume_m3", 0.0)))
        self.base_daily_edit = QLineEdit(
            str(self.seed.get("base_daily_volume_m3", 0.0))
        )
        self.initial_container_check = QCheckBox(
            "Container/bin is present at the department when simulation starts"
        )
        self.initial_container_check.setChecked(
            bool(self.seed.get("initial_container_present", True))
        )
        self.shared_container_check = QCheckBox(
            "Share this physical bin/container with other departments"
        )
        self.shared_container_check.setChecked(
            bool(self.seed.get("shared_container", False))
        )
        self.shared_container_group_edit = QLineEdit(
            str(
                self.seed.get(
                    "shared_container_group", self.seed.get("shared_container_id", "")
                )
            )
        )
        self.shared_container_group_edit.setPlaceholderText(
            "Optional ID, e.g. Ground Floor Clinical Bin A. Blank shares by pickup location."
        )

        schedule_row = QHBoxLayout()
        self.schedule_summary = QLabel()
        self.schedule_summary.setWordWrap(True)

        edit_times_btn = QPushButton("Edit times...")
        clear_times_btn = QPushButton("Clear")

        edit_times_btn.clicked.connect(self.edit_times)
        clear_times_btn.clicked.connect(self.clear_times)

        self.edit_times_btn = edit_times_btn
        self.clear_times_btn = clear_times_btn

        schedule_row.addWidget(self.schedule_summary, 1)
        schedule_row.addWidget(edit_times_btn)
        schedule_row.addWidget(clear_times_btn)

        form.addRow("Waste stream", self.name_combo)
        form.addRow("Generation mode", self.mode_combo)
        form.addRow("Frequency per day", self.frequency_edit)
        form.addRow("Volume per event m³", self.volume_edit)
        form.addRow("Threshold volume m³", self.threshold_edit)
        form.addRow("Base daily volume m³", self.base_daily_edit)
        form.addRow("Scheduled times", schedule_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_combo.currentTextChanged.connect(self.update_field_state)
        self.shared_container_check.toggled.connect(self.update_field_state)

        self.refresh_schedule_summary()
        self.update_field_state()
        _polish_dialog(self)

    def edit_times(self):
        dialog = ScheduledTimesDialog(self, self.scheduled_times)
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.scheduled_times = list(dialog.result)
            self.refresh_schedule_summary()

    def clear_times(self):
        self.scheduled_times = []
        self.refresh_schedule_summary()

    def refresh_schedule_summary(self):
        if not self.scheduled_times:
            self.schedule_summary.setText("No times selected")
        elif len(self.scheduled_times) <= 6:
            self.schedule_summary.setText(", ".join(self.scheduled_times))
        else:
            self.schedule_summary.setText(f"{len(self.scheduled_times)} times selected")

    def update_field_state(self):
        mode = self.mode_combo.currentText().strip()

        uses_schedule = mode in {
            "scheduled",
            "scheduled_threshold",
            "scheduled_sporadic",
        }
        uses_threshold = mode in {
            "threshold",
            "hybrid",
            "scheduled_threshold",
        }
        uses_continuous = mode in {
            "continuous",
            "hybrid",
        }
        uses_sporadic = mode in {
            "sporadic",
            "hybrid",
            "scheduled_sporadic",
        }

        # Department waste-stream settings feed the Waste task generator.
        # Threshold mode can still accumulate volume from discrete events, so
        # frequency/volume must remain editable for threshold streams as well
        # as sporadic streams.  Scheduled streams can use volume/event too.
        uses_event_volume = uses_sporadic or uses_threshold or uses_schedule

        self.schedule_summary.setEnabled(uses_schedule)
        self.edit_times_btn.setEnabled(uses_schedule)
        self.clear_times_btn.setEnabled(uses_schedule)

        self.threshold_edit.setEnabled(uses_threshold)
        self.base_daily_edit.setEnabled(uses_continuous or uses_threshold)
        self.frequency_edit.setEnabled(uses_event_volume)
        self.volume_edit.setEnabled(uses_event_volume)
        self.shared_container_group_edit.setEnabled(
            self.shared_container_check.isChecked()
        )

    def accept(self):
        try:
            name = self.name_combo.currentText().strip()
            if not name:
                raise ValueError("Waste stream is required")

            self.result = {
                "name": name,
                "generation_mode": self.mode_combo.currentText().strip(),
                "frequency_per_day": float(self.frequency_edit.text() or 0.0),
                "volume_per_event_m3": float(self.volume_edit.text() or 0.0),
                "threshold_volume_m3": float(self.threshold_edit.text() or 0.0),
                "base_daily_volume_m3": float(self.base_daily_edit.text() or 0.0),
                "scheduled_times": list(self.scheduled_times),
                "initial_container_present": self.initial_container_check.isChecked(),
                "shared_container": self.shared_container_check.isChecked(),
                "shared_container_group": self.shared_container_group_edit.text().strip(),
            }

            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid waste stream settings", str(exc))


class DepartmentEditorDialog(QDialog):
    DAYS = [
        ("mon", "Mon"),
        ("tue", "Tue"),
        ("wed", "Wed"),
        ("thu", "Thu"),
        ("fri", "Fri"),
        ("sat", "Sat"),
        ("sun", "Sun"),
    ]

    def __init__(
        self,
        parent,
        location_names,
        waste_stream_names,
        current_floor=0,
        seed=None,
        default_department_id="D1",
        group_resolver=None,
        default_x=0.0,
        default_y=0.0,
        task_generation_categories=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Department")
        self.result = None
        self.seed = seed or {}
        self.location_names = sorted(location_names)
        self.waste_stream_names = sorted(waste_stream_names)
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.task_generation_categories = list(task_generation_categories or [])
        self.category_location_selections = self._normalise_task_generation_locations()
        self.category_location_summaries = {}
        self.category_suffix_edits = {}
        self.category_place_location_buttons = {}
        self.category_pending_locations = {}

        self.selected_waste_streams = self._normalise_department_waste_streams(
            self.seed.get("waste_streams", [])
        )

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.id_edit = QLineEdit(self.seed.get("id", default_department_id))
        self.name_edit = QLineEdit(self.seed.get("name", ""))
        self.floor_label = QLabel(str(int(self.seed.get("floor", current_floor))))
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(self.seed.get("enabled", True)))

        self.bed_count_edit = QLineEdit(str(self.seed.get("bed_count", 0)))
        self.turnover_edit = QLineEdit(str(self.seed.get("patient_turnover", 0.0)))
        self.staff_count_edit = QLineEdit(str(self.seed.get("staff_count", 0)))
        operating_start_seed = str(
            self.seed.get("operating_start_time", "00:00") or "00:00"
        )
        operating_end_seed = str(self.seed.get("operating_end_time", "") or "").strip()
        if not operating_end_seed:
            operating_end_seed = self._derive_operating_end_from_legacy_hours(
                operating_start_seed,
                self.seed.get("hours_operated_per_day", 24),
            )

        self.operating_start_edit = QLineEdit(operating_start_seed)
        self.operating_end_edit = QLineEdit(operating_end_seed)
        self.operating_end_edit.setPlaceholderText("HH:MM, e.g. 17:00 or 24:00")
        self.operating_hours_label = QLabel("24.00 h")

        days_widget = QWidget()
        days_layout = QHBoxLayout(days_widget)
        days_layout.setContentsMargins(0, 0, 0, 0)
        active_days = set(
            self.seed.get("days_active", ["mon", "tue", "wed", "thu", "fri"])
        )
        self.day_checks = {}
        for key, label in self.DAYS:
            chk = QCheckBox(label)
            chk.setChecked(key in active_days)
            self.day_checks[key] = chk
            days_layout.addWidget(chk)

        waste_row = QHBoxLayout()
        self.waste_summary = QLabel("None selected")
        self.waste_summary.setWordWrap(True)
        waste_btn = QPushButton("Select...")
        waste_btn.clicked.connect(self._pick_waste_streams)
        waste_row.addWidget(self.waste_summary, 1)
        waste_row.addWidget(waste_btn)

        self.x_edit = QLineEdit(str(self.seed.get("x", default_x)))
        self.y_edit = QLineEdit(str(self.seed.get("y", default_y)))

        form.addRow("Department ID", self.id_edit)
        form.addRow("Department name", self.name_edit)
        form.addRow("Floor", self.floor_label)
        form.addRow("Status", self.enabled_check)
        form.addRow("Bed count", self.bed_count_edit)
        form.addRow("Patient turnover", self.turnover_edit)
        form.addRow("Staff count", self.staff_count_edit)
        form.addRow("Operating start", self.operating_start_edit)
        form.addRow("Operating end", self.operating_end_edit)
        form.addRow("Calculated operating hours", self.operating_hours_label)
        form.addRow("Days active", days_widget)
        form.addRow("Assigned waste streams", waste_row)
        waste_help = QLabel(
            "Waste streams drive Waste task generation. Configure the generation mode, "
            "frequency, volume per event, threshold and scheduled times here rather than "
            "in the Task Generation category override."
        )
        waste_help.setWordWrap(True)
        form.addRow("", waste_help)

        for (
            category_key,
            category_label,
            default_suffix,
        ) in self.task_generation_categories:
            row = QHBoxLayout()

            summary = QLabel("None selected")
            summary.setWordWrap(True)

            select_btn = QPushButton("Select...")
            select_btn.clicked.connect(
                lambda _=False, key=category_key: self._pick_category_locations(key)
            )

            suffix_edit = QLineEdit(
                str(
                    self.seed.get("task_generation_location_suffixes", {}).get(
                        category_key, default_suffix
                    )
                )
            )
            suffix_edit.setFixedWidth(90)

            place_btn = QPushButton("Place...")
            place_btn.setToolTip("Place location on the DXF/editor scene")
            place_btn.clicked.connect(
                lambda _=False, key=category_key: self._place_category_location(key)
            )

            row.addWidget(summary, 1)
            row.addWidget(select_btn)
            row.addWidget(QLabel("Suffix"))
            row.addWidget(suffix_edit)
            row.addWidget(place_btn)

            self.category_location_summaries[category_key] = summary
            self.category_suffix_edits[category_key] = suffix_edit
            self.category_place_location_buttons[category_key] = place_btn

            form.addRow(f"{category_label} pickup / drop-off", row)

        form.addRow("X", self.x_edit)
        form.addRow("Y", self.y_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.operating_start_edit.textChanged.connect(
            self._refresh_operating_hours_label
        )
        self.operating_end_edit.textChanged.connect(self._refresh_operating_hours_label)
        self._refresh_all_category_location_summaries()
        self._refresh_waste_summary()
        self._refresh_operating_hours_label()
        _polish_dialog(self)

    def _parse_hhmm_minutes_for_operating_hours(
        self, value, field_name, allow_blank=False
    ):
        text = str(value or "").strip()
        if not text and allow_blank:
            return None
        try:
            parts = text.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if hour == 24 and minute == 0:
                return 24 * 60
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour * 60) + minute
        except Exception:
            pass
        raise ValueError(f"{field_name} must be HH:MM, for example 08:00")

    def _derive_operating_end_from_legacy_hours(self, start_text, hours_value):
        try:
            start = self._parse_hhmm_minutes_for_operating_hours(
                start_text, "Operating start", allow_blank=False
            )
            hours = max(0.0, min(float(hours_value or 24.0), 24.0))
            if hours >= 24.0:
                return "24:00"
            end = (int(start or 0) + int(round(hours * 60.0))) % (24 * 60)
            return f"{end // 60:02d}:{end % 60:02d}"
        except Exception:
            return "24:00"

    def _calculate_operating_hours_per_day(self, start_text=None, end_text=None):
        start = self._parse_hhmm_minutes_for_operating_hours(
            self.operating_start_edit.text() if start_text is None else start_text,
            "Operating start",
            allow_blank=False,
        )
        end = self._parse_hhmm_minutes_for_operating_hours(
            self.operating_end_edit.text() if end_text is None else end_text,
            "Operating end",
            allow_blank=False,
        )
        if end == start:
            return 24.0
        if end < start:
            end += 24 * 60
        return max(0.0, min((end - start) / 60.0, 24.0))

    def _refresh_operating_hours_label(self):
        try:
            hours = self._calculate_operating_hours_per_day()
            self.operating_hours_label.setText(f"{hours:.2f} h")
        except Exception:
            self.operating_hours_label.setText("Invalid start/end time")

    def _editor_window_for_department_placement(self):
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "start_department_location_placement"):
                return parent
            parent = parent.parent() if hasattr(parent, "parent") else None
        return None

    def _place_category_location(self, category_key):
        parent = self._editor_window_for_department_placement()

        if parent is None:
            QMessageBox.critical(
                self,
                "Placement unavailable",
                "The editor does not support graphical location placement.",
            )
            return

        dept_id = self.id_edit.text().strip()
        if not dept_id:
            QMessageBox.critical(
                self, "Missing department ID", "Enter a department ID first."
            )
            return

        suffix = self.category_suffix_edits[category_key].text().strip()
        location_name = self._next_category_location_name(category_key)

        self.hide()

        parent.start_department_location_placement(
            location_name=location_name,
            category_key=category_key,
            callback=self._finish_category_location_placement,
            return_dialog=self,
        )

    def _finish_category_location_placement(self, category_key, location_payload):
        location_name = str(location_payload.get("name", "")).strip()
        if not location_name:
            self.show()
            self.raise_()
            self.activateWindow()
            return

        self._add_category_location_selection(category_key, location_name)

        if location_name not in self.location_names:
            self.location_names.append(location_name)
            self.location_names = sorted(set(self.location_names))

        # Keep a copy of the placed location in the dialog result path.  The
        # main editor normally creates the location immediately when the scene
        # is clicked, but this pending list makes the department save robust if
        # the location is created by a deferred/dialog-driven path.
        self.category_pending_locations[location_name] = dict(location_payload)

        # Persist immediately into every backing model that might own this editor:
        # 1) DepartmentListDialog.items when opened from Departments.
        # 2) AMRGraphEditor.store.data when opened directly from the canvas.
        # This prevents OK/reopen from rebuilding the dialog from stale data.
        self._register_placed_category_location(category_key, location_payload)

        self._refresh_category_location_summary(category_key)

        self.show()
        self.raise_()
        self.activateWindow()

    def _add_category_location_selection(self, category_key, location_name):
        category_key = str(category_key or "").strip()
        location_name = str(location_name or "").strip()
        if not category_key or not location_name:
            return
        selected = self.category_location_selections.setdefault(category_key, [])
        existing = {str(x).strip() for x in selected if str(x).strip()}
        if location_name not in existing:
            selected.append(location_name)
        self.category_location_selections[category_key] = sorted(
            {str(x).strip() for x in selected if str(x).strip()}
        )

    def _candidate_department_ids_for_placement(self):
        candidates = []
        for value in (
            self.id_edit.text().strip(),
            getattr(self, "original_department_id", ""),
            self.seed.get("id", "") if isinstance(self.seed, dict) else "",
        ):
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        return candidates

    def _candidate_department_names_for_placement(self):
        candidates = []
        for value in (
            self.name_edit.text().strip(),
            getattr(self, "original_department_name", ""),
            self.seed.get("name", "") if isinstance(self.seed, dict) else "",
        ):
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        return candidates

    @staticmethod
    def _ensure_category_location_entry(dept, category_key, location_name):
        if not isinstance(dept, dict):
            return False
        category_key = str(category_key or "").strip()
        location_name = str(location_name or "").strip()
        if not category_key or not location_name:
            return False

        category_locations = dept.setdefault("task_generation_locations", {})
        if not isinstance(category_locations, dict):
            category_locations = {}
            dept["task_generation_locations"] = category_locations

        entry = category_locations.setdefault(category_key, {})
        if not isinstance(entry, dict):
            entry = {"pickup_dropoff_locations": list(entry or [])}
            category_locations[category_key] = entry

        selected = entry.get("pickup_dropoff_locations", entry.get("locations", []))
        if isinstance(selected, str):
            selected = [selected]
        elif not isinstance(selected, list):
            selected = list(selected or [])

        cleaned = []
        seen = set()
        for item in selected + [location_name]:
            text = str(item or "").strip()
            if text and text not in seen:
                cleaned.append(text)
                seen.add(text)

        entry["pickup_dropoff_locations"] = cleaned
        entry["locations"] = list(cleaned)
        return True

    def _register_placed_category_location(self, category_key, location_payload):
        location_payload = dict(location_payload or {})
        location_name = str(location_payload.get("name", "") or "").strip()
        if not location_name:
            return

        dept_ids = self._candidate_department_ids_for_placement()
        dept_names = self._candidate_department_names_for_placement()

        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "register_placed_department_location"):
                for dept_id in dept_ids:
                    try:
                        parent.register_placed_department_location(
                            dept_id,
                            category_key,
                            location_payload,
                        )
                    except Exception:
                        pass

            store = getattr(parent, "store", None)
            data = getattr(store, "data", None) if store is not None else None
            if isinstance(data, dict):
                for dept in data.get("departments", []) or []:
                    current_id = str(dept.get("id", "") or "").strip()
                    current_name = str(dept.get("name", "") or "").strip()
                    if (current_id and current_id in dept_ids) or (
                        current_name and current_name in dept_names
                    ):
                        self._ensure_category_location_entry(
                            dept,
                            category_key,
                            location_name,
                        )

            parent = parent.parent() if hasattr(parent, "parent") else None

    def _next_category_location_name(self, category_key):
        dept_id = self.id_edit.text().strip()
        suffix = self.category_suffix_edits[category_key].text().strip()
        base_name = f"{dept_id}{suffix}"

        used = set(self.location_names)

        for locations in self.category_location_selections.values():
            used.update(str(x).strip() for x in locations if str(x).strip())

        if base_name not in used:
            return base_name

        counter = 2
        while True:
            candidate = f"{base_name}_{counter}"
            if candidate not in used:
                return candidate
            counter += 1

    def _normalise_task_generation_locations(self):
        result = {}

        existing = self.seed.get("task_generation_locations", {})
        if isinstance(existing, dict):
            for category_key, item in existing.items():
                if isinstance(item, dict):
                    locations = item.get(
                        "pickup_dropoff_locations", item.get("locations", [])
                    )
                else:
                    locations = item

                result[str(category_key)] = [
                    str(x).strip() for x in locations or [] if str(x).strip()
                ]

        return result

    def _refresh_category_location_summary(self, category_key):
        summary = self.category_location_summaries.get(category_key)
        if summary is None:
            return

        values = self.category_location_selections.get(category_key, [])
        if not values:
            summary.setText("None selected")
        elif len(values) <= 4:
            summary.setText(", ".join(values))
        else:
            summary.setText(f"{len(values)} selected")

    def _refresh_all_category_location_summaries(self):
        for category_key, *_ in self.task_generation_categories:
            self._refresh_category_location_summary(category_key)

    def _pick_category_locations(self, category_key):
        picker = MultiSelectPicker(
            self,
            "Select pickup / drop-off locations",
            self.location_names,
            selected=self.category_location_selections.get(category_key, []),
            group_resolver=self.group_resolver,
        )

        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.category_location_selections[category_key] = sorted(picker.result)
            self._refresh_category_location_summary(category_key)

    def _normalise_department_waste_streams(self, value):
        result = []

        for item in value or []:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                result.append(
                    {
                        "name": name,
                        "generation_mode": str(
                            item.get("generation_mode", "threshold")
                        ),
                        "frequency_per_day": float(item.get("frequency_per_day", 0.0)),
                        "volume_per_event_m3": float(
                            item.get("volume_per_event_m3", 0.0)
                        ),
                        "threshold_volume_m3": float(
                            item.get("threshold_volume_m3", 0.0)
                        ),
                        "base_daily_volume_m3": float(
                            item.get("base_daily_volume_m3", 0.0)
                        ),
                        "scheduled_times": list(item.get("scheduled_times", [])),
                        "initial_container_present": bool(
                            item.get("initial_container_present", True)
                        ),
                        "shared_container": bool(item.get("shared_container", False)),
                        "shared_container_group": str(
                            item.get(
                                "shared_container_group",
                                item.get("shared_container_id", ""),
                            )
                        ).strip(),
                    }
                )
            else:
                name = str(item).strip()
                if name:
                    result.append(
                        {
                            "name": name,
                            "generation_mode": "threshold",
                            "frequency_per_day": 0.0,
                            "volume_per_event_m3": 0.0,
                            "threshold_volume_m3": 0.0,
                            "base_daily_volume_m3": 0.0,
                            "scheduled_times": [],
                            "initial_container_present": True,
                            "shared_container": False,
                            "shared_container_group": "",
                        }
                    )

        return result

    def _refresh_waste_summary(self):
        names = [
            str(x.get("name", "")).strip()
            for x in self.selected_waste_streams
            if str(x.get("name", "")).strip()
        ]

        if not names:
            self.waste_summary.setText("None selected")
        elif len(names) <= 4:
            self.waste_summary.setText(", ".join(names))
        else:
            self.waste_summary.setText(f"{len(names)} selected")

    def _pick_waste_streams(self):
        dialog = DepartmentWasteStreamSettingsDialog(
            self,
            self.waste_stream_names,
            self.selected_waste_streams,
        )

        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.selected_waste_streams = list(dialog.result)
            self._refresh_waste_summary()

    def _normalise_task_generation_locations(self):
        result = {}

        existing = self.seed.get("task_generation_locations", {})
        if isinstance(existing, dict):
            for category_key, item in existing.items():
                if isinstance(item, dict):
                    locations = item.get(
                        "pickup_dropoff_locations", item.get("locations", [])
                    )
                else:
                    locations = item

                result[str(category_key)] = [
                    str(x).strip() for x in (locations or []) if str(x).strip()
                ]

        # Legacy migration from old waste fields
        legacy = []

        for value in self.seed.get("waste_pickup_locations", []):
            text = str(value).strip()
            if text and text not in legacy:
                legacy.append(text)

        waste_cfg = self.seed.get("waste", {}) or {}
        for key in ["pickup_location", "dropoff_location"]:
            text = str(waste_cfg.get(key, "")).strip()
            if text and text not in legacy:
                legacy.append(text)

        if legacy and "waste" not in result:
            result["waste"] = legacy

        return result

    def _location_summary_text(self, locations):
        if not locations:
            return "None selected"
        if len(locations) <= 4:
            return ", ".join(locations)
        return f"{len(locations)} selected"

    def _refresh_category_location_summary(self, category_key):
        summary = self.category_location_summaries.get(category_key)
        if summary is None:
            return

        locations = self.category_location_selections.get(category_key, [])
        summary.setText(self._location_summary_text(locations))

    def _pick_category_locations(self, category_key):
        picker = MultiSelectPicker(
            self,
            "Select pickup / drop-off locations",
            self.location_names,
            selected=self.category_location_selections.get(category_key, []),
            group_resolver=self.group_resolver,
        )

        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.category_location_selections[category_key] = sorted(picker.result)
            self._refresh_category_location_summary(category_key)

    def _validate_hhmm_or_blank(self, value, field_name, allow_blank=False):
        text = str(value or "").strip()
        if not text and allow_blank:
            return ""
        try:
            parts = text.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if hour == 24 and minute == 0:
                return "24:00"
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        except Exception:
            pass
        raise ValueError(f"{field_name} must be HH:MM, for example 08:00")

    def accept(self):
        try:
            dept_id = self.id_edit.text().strip()
            name = self.name_edit.text().strip()
            if not dept_id:
                raise ValueError("Department ID is required")
            if not name:
                raise ValueError("Department name is required")

            days_active = [
                key for key, _ in self.DAYS if self.day_checks[key].isChecked()
            ]
            if not days_active:
                raise ValueError("Select at least one active day")

            dept_id = self.id_edit.text().strip()
            floor = int(self.floor_label.text())
            x = float(self.x_edit.text())
            y = float(self.y_edit.text())

            location_suffixes = {}

            for (
                category_key,
                _category_label,
                _default_suffix,
            ) in self.task_generation_categories:
                location_suffixes[category_key] = (
                    self.category_suffix_edits[category_key].text().strip()
                )

            create_locations = list(self.category_pending_locations.values())

            operating_start_time = self._validate_hhmm_or_blank(
                self.operating_start_edit.text(), "Operating start", allow_blank=False
            )
            operating_end_time = self._validate_hhmm_or_blank(
                self.operating_end_edit.text(), "Operating end", allow_blank=False
            )

            self.result = {
                "id": dept_id,
                "name": name,
                "floor": int(self.floor_label.text()),
                "enabled": self.enabled_check.isChecked(),
                "bed_count": int(float(self.bed_count_edit.text())),
                "patient_turnover": float(self.turnover_edit.text()),
                "staff_count": int(float(self.staff_count_edit.text())),
                # Kept in JSON as a derived compatibility field for older reporting/simulator code.
                "hours_operated_per_day": round(
                    self._calculate_operating_hours_per_day(
                        operating_start_time, operating_end_time
                    ),
                    4,
                ),
                "operating_start_time": operating_start_time,
                "operating_end_time": operating_end_time,
                "days_active": days_active,
                "waste_streams": [dict(x) for x in self.selected_waste_streams],
                "task_generation_locations": {
                    category_key: {
                        "pickup_dropoff_locations": [
                            str(x).strip()
                            for x in self.category_location_selections.get(
                                category_key, []
                            )
                            if str(x).strip()
                        ]
                    }
                    for category_key, *_ in self.task_generation_categories
                },
                "task_generation_location_suffixes": location_suffixes,
                "_create_locations": create_locations,
                "x": float(self.x_edit.text()),
                "y": float(self.y_edit.text()),
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid department", str(exc))


class BulkDepartmentWasteStreamControlDialog(QDialog):
    """Bulk add/remove/edit department waste streams for selected departments.

    Checked      = assign/update the stream on every selected department.
    Unchecked    = remove the stream from every selected department.
    Part checked = leave mixed existing assignments unchanged.
    """

    MODES = [
        "scheduled",
        "threshold",
        "continuous",
        "sporadic",
        "hybrid",
        "scheduled_threshold",
        "scheduled_sporadic",
    ]

    DEFAULT_STREAM_SETTINGS = {
        "generation_mode": "threshold",
        "frequency_per_day": 0.0,
        "volume_per_event_m3": 0.0,
        "threshold_volume_m3": 0.0,
        "base_daily_volume_m3": 0.0,
        "scheduled_times": [],
    }

    def __init__(self, parent, waste_stream_names, departments):
        super().__init__(parent)
        self.setWindowTitle("Manage waste streams for selected departments")
        self.resize(1180, 650)

        self.global_waste_stream_names = {
            str(x).strip() for x in waste_stream_names if str(x).strip()
        }
        self.departments = [dict(x) for x in departments or []]

        # Include deleted/orphaned stream names that still exist on the selected
        # departments. They must remain visible so they can be unchecked and
        # removed even after the global waste stream definition has been deleted.
        assigned_names = set()
        for dept in self.departments:
            assigned_names.update(self._stream_names_for_department(dept))

        self.waste_stream_names = sorted(
            self.global_waste_stream_names | assigned_names
        )
        self.orphan_waste_stream_names = sorted(
            assigned_names - self.global_waste_stream_names
        )
        self.result = None
        self._row_widgets = {}

        layout = QVBoxLayout(self)

        help_label = QLabel(
            "Tick a stream to assign it to all selected departments. "
            "Untick a stream to remove it from all selected departments. "
            "A partially ticked stream is currently assigned to only some departments; "
            "leave it partially ticked to make no assignment/settings change. "
            "For checked streams, the generation settings below are applied to every "
            "selected department at the same time. Deleted/orphaned streams are shown "
            "so they can be unchecked and removed."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTreeWidget()
        self.table.setColumnCount(12)
        self.table.setHeaderLabels(
            [
                "Waste stream",
                "Status",
                "Currently assigned",
                "Bulk action",
                "Generation mode",
                "Frequency / day",
                "Volume / event m³",
                "Threshold m³",
                "Base daily m³",
                "Scheduled times",
                "Initial container",
                "Shared bin group",
            ]
        )
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setRootIsDecorated(False)
        self.table.header().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table, 1)

        tools = QHBoxLayout()
        layout.addLayout(tools)

        check_all_btn = QPushButton("Assign all")
        clear_all_btn = QPushButton("Remove all")
        unchanged_btn = QPushButton("Leave mixed unchanged")

        check_all_btn.clicked.connect(lambda: self._set_all(Qt.Checked))
        clear_all_btn.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        unchanged_btn.clicked.connect(self._restore_partial_states)

        tools.addWidget(check_all_btn)
        tools.addWidget(clear_all_btn)
        tools.addWidget(unchanged_btn)
        tools.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._initial_partial_streams = set()
        self._refresh_table()
        _polish_dialog(self)

    def _stream_names_for_department(self, dept):
        names = set()
        for item in dept.get("waste_streams", []) or []:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            else:
                name = str(item).strip()
            if name:
                names.add(name)
        return names

    def _normalise_stream_item(self, item):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                return None
            return {
                "name": name,
                "generation_mode": str(
                    item.get(
                        "generation_mode",
                        self.DEFAULT_STREAM_SETTINGS["generation_mode"],
                    )
                    or self.DEFAULT_STREAM_SETTINGS["generation_mode"]
                ),
                "frequency_per_day": float(item.get("frequency_per_day", 0.0) or 0.0),
                "volume_per_event_m3": float(
                    item.get("volume_per_event_m3", 0.0) or 0.0
                ),
                "threshold_volume_m3": float(
                    item.get("threshold_volume_m3", 0.0) or 0.0
                ),
                "base_daily_volume_m3": float(
                    item.get("base_daily_volume_m3", 0.0) or 0.0
                ),
                "scheduled_times": list(item.get("scheduled_times", []) or []),
                "initial_container_present": bool(
                    item.get("initial_container_present", True)
                ),
                "shared_container": bool(item.get("shared_container", False)),
                "shared_container_group": str(
                    item.get(
                        "shared_container_group", item.get("shared_container_id", "")
                    )
                ).strip(),
            }

        name = str(item).strip()
        if not name:
            return None
        return {
            "name": name,
            **self.DEFAULT_STREAM_SETTINGS,
            "initial_container_present": True,
            "shared_container": False,
            "shared_container_group": "",
        }

    def _first_settings_for_stream(self, stream_name):
        """Use the first existing department settings as the edit seed."""
        for dept in self.departments:
            for item in dept.get("waste_streams", []) or []:
                normalised = self._normalise_stream_item(item)
                if not normalised:
                    continue
                if str(normalised.get("name", "")).strip() == stream_name:
                    return normalised
        return {"name": stream_name, **self.DEFAULT_STREAM_SETTINGS}

    def _make_number_edit(self, value):
        edit = QLineEdit(str(value))
        edit.setMinimumWidth(90)
        return edit

    def _refresh_table(self):
        self.table.clear()
        self._row_widgets = {}
        self._initial_partial_streams = set()

        dept_count = len(self.departments)
        orphan_count = len(self.orphan_waste_stream_names)
        if orphan_count:
            self.summary_label.setText(
                f"Selected departments: {dept_count} | "
                f"Deleted/orphaned assigned streams: {orphan_count}"
            )
        else:
            self.summary_label.setText(f"Selected departments: {dept_count}")

        for stream_name in self.waste_stream_names:
            assigned_count = sum(
                1
                for dept in self.departments
                if stream_name in self._stream_names_for_department(dept)
            )

            is_orphan = stream_name not in self.global_waste_stream_names
            item = QTreeWidgetItem(
                [
                    stream_name,
                    (
                        "Deleted / not in global waste streams"
                        if is_orphan
                        else "Configured"
                    ),
                    f"{assigned_count} / {dept_count}",
                    (
                        "Preserve or remove"
                        if is_orphan
                        else (
                            "Apply settings to all"
                            if assigned_count == dept_count and dept_count > 0
                            else (
                                "Assign/update all"
                                if assigned_count == 0
                                else "Mixed - leave unchanged unless checked/unchecked"
                            )
                        )
                    ),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            item.setData(0, Qt.UserRole, stream_name)
            item.setFlags(
                item.flags()
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsEnabled
                | Qt.ItemIsSelectable
            )

            if assigned_count <= 0:
                state = Qt.Unchecked
            elif assigned_count >= dept_count:
                state = Qt.Checked
            else:
                state = Qt.PartiallyChecked
                self._initial_partial_streams.add(stream_name)

            item.setCheckState(0, state)
            self.table.addTopLevelItem(item)

            seed = self._first_settings_for_stream(stream_name)
            mode_combo = QComboBox()
            mode_combo.addItems(self.MODES)
            mode = str(seed.get("generation_mode", "threshold") or "threshold")
            if mode not in self.MODES:
                mode = "threshold"
            mode_combo.setCurrentText(mode)

            frequency_edit = self._make_number_edit(seed.get("frequency_per_day", 0.0))
            volume_edit = self._make_number_edit(seed.get("volume_per_event_m3", 0.0))
            threshold_edit = self._make_number_edit(
                seed.get("threshold_volume_m3", 0.0)
            )
            base_daily_edit = self._make_number_edit(
                seed.get("base_daily_volume_m3", 0.0)
            )
            initial_container_check = QCheckBox("Present")
            initial_container_check.setChecked(
                bool(seed.get("initial_container_present", True))
            )
            shared_group_edit = QLineEdit(
                str(
                    seed.get(
                        "shared_container_group", seed.get("shared_container_id", "")
                    )
                    or ""
                )
            )
            shared_group_edit.setPlaceholderText("Optional shared bin ID")

            scheduled_times = list(seed.get("scheduled_times", []) or [])
            schedule_label = QLabel()
            schedule_label.setWordWrap(True)
            edit_times_btn = QPushButton("Edit...")
            clear_times_btn = QPushButton("Clear")

            schedule_widget = QWidget()
            schedule_row = QHBoxLayout(schedule_widget)
            schedule_row.setContentsMargins(0, 0, 0, 0)
            schedule_row.addWidget(schedule_label, 1)
            schedule_row.addWidget(edit_times_btn)
            schedule_row.addWidget(clear_times_btn)

            self._row_widgets[stream_name] = {
                "mode_combo": mode_combo,
                "frequency_edit": frequency_edit,
                "volume_edit": volume_edit,
                "threshold_edit": threshold_edit,
                "base_daily_edit": base_daily_edit,
                "scheduled_times": scheduled_times,
                "schedule_label": schedule_label,
                "edit_times_btn": edit_times_btn,
                "clear_times_btn": clear_times_btn,
                "initial_container_check": initial_container_check,
                "shared_group_edit": shared_group_edit,
            }

            edit_times_btn.clicked.connect(
                lambda _checked=False, name=stream_name: self._edit_times_for_stream(
                    name
                )
            )
            clear_times_btn.clicked.connect(
                lambda _checked=False, name=stream_name: self._clear_times_for_stream(
                    name
                )
            )
            mode_combo.currentTextChanged.connect(
                lambda _value, name=stream_name: self._update_stream_field_state(name)
            )

            self.table.setItemWidget(item, 4, mode_combo)
            self.table.setItemWidget(item, 5, frequency_edit)
            self.table.setItemWidget(item, 6, volume_edit)
            self.table.setItemWidget(item, 7, threshold_edit)
            self.table.setItemWidget(item, 8, base_daily_edit)
            self.table.setItemWidget(item, 9, schedule_widget)
            self.table.setItemWidget(item, 10, initial_container_check)
            self.table.setItemWidget(item, 11, shared_group_edit)

            self._refresh_schedule_summary(stream_name)
            self._update_stream_field_state(stream_name)

            if is_orphan:
                # Orphaned stream definitions no longer exist globally. They can
                # be preserved or removed, but not bulk-edited/reassigned.
                for widget in (
                    mode_combo,
                    frequency_edit,
                    volume_edit,
                    threshold_edit,
                    base_daily_edit,
                    edit_times_btn,
                    clear_times_btn,
                    initial_container_check,
                    shared_group_edit,
                ):
                    widget.setEnabled(False)
                schedule_label.setEnabled(False)

        for col in range(self.table.columnCount()):
            self.table.resizeColumnToContents(col)

    def _set_all(self, state):
        for idx in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(idx)
            item.setCheckState(0, state)

    def _restore_partial_states(self):
        for idx in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(idx)
            stream_name = str(item.data(0, Qt.UserRole) or "").strip()
            if stream_name in self._initial_partial_streams:
                item.setCheckState(0, Qt.PartiallyChecked)

    def _refresh_schedule_summary(self, stream_name):
        widgets = self._row_widgets.get(stream_name, {})
        label = widgets.get("schedule_label")
        if label is None:
            return
        times = list(widgets.get("scheduled_times", []) or [])
        if not times:
            label.setText("No times")
        elif len(times) <= 3:
            label.setText(", ".join(times))
        else:
            label.setText(f"{len(times)} times")

    def _edit_times_for_stream(self, stream_name):
        widgets = self._row_widgets.get(stream_name, {})
        dialog = ScheduledTimesDialog(self, widgets.get("scheduled_times", []))
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            widgets["scheduled_times"] = list(dialog.result)
            self._refresh_schedule_summary(stream_name)

    def _clear_times_for_stream(self, stream_name):
        widgets = self._row_widgets.get(stream_name, {})
        widgets["scheduled_times"] = []
        self._refresh_schedule_summary(stream_name)

    def _update_stream_field_state(self, stream_name):
        widgets = self._row_widgets.get(stream_name, {})
        mode_combo = widgets.get("mode_combo")
        if mode_combo is None:
            return

        mode = mode_combo.currentText().strip()
        uses_schedule = mode in {
            "scheduled",
            "scheduled_threshold",
            "scheduled_sporadic",
        }
        uses_threshold = mode in {
            "threshold",
            "hybrid",
            "scheduled_threshold",
        }
        uses_continuous = mode in {
            "continuous",
            "hybrid",
        }
        uses_sporadic = mode in {
            "sporadic",
            "hybrid",
            "scheduled_sporadic",
        }

        # Threshold waste streams can be filled by event counts
        # (frequency_per_day × volume_per_event_m3), so these fields are not
        # sporadic-only.  Scheduled streams may also use volume per event.
        uses_event_volume = uses_sporadic or uses_threshold or uses_schedule

        for key in ("schedule_label", "edit_times_btn", "clear_times_btn"):
            widget = widgets.get(key)
            if widget is not None:
                widget.setEnabled(uses_schedule)

        if widgets.get("threshold_edit") is not None:
            widgets["threshold_edit"].setEnabled(uses_threshold)
        if widgets.get("base_daily_edit") is not None:
            widgets["base_daily_edit"].setEnabled(uses_continuous or uses_threshold)
        if widgets.get("frequency_edit") is not None:
            widgets["frequency_edit"].setEnabled(uses_event_volume)
        if widgets.get("volume_edit") is not None:
            widgets["volume_edit"].setEnabled(uses_event_volume)

    def _settings_for_stream(self, stream_name):
        widgets = self._row_widgets.get(stream_name, {})
        return {
            "generation_mode": widgets["mode_combo"].currentText().strip(),
            "frequency_per_day": float(widgets["frequency_edit"].text() or 0.0),
            "volume_per_event_m3": float(widgets["volume_edit"].text() or 0.0),
            "threshold_volume_m3": float(widgets["threshold_edit"].text() or 0.0),
            "base_daily_volume_m3": float(widgets["base_daily_edit"].text() or 0.0),
            "scheduled_times": list(widgets.get("scheduled_times", []) or []),
            "initial_container_present": widgets["initial_container_check"].isChecked(),
            "shared_container": bool(widgets["shared_group_edit"].text().strip()),
            "shared_container_group": widgets["shared_group_edit"].text().strip(),
        }

    def accept(self):
        add_streams = []
        remove_streams = []
        unchanged_streams = []
        update_stream_settings = {}

        try:
            for idx in range(self.table.topLevelItemCount()):
                item = self.table.topLevelItem(idx)
                stream_name = str(item.data(0, Qt.UserRole) or "").strip()
                if not stream_name:
                    continue

                state = item.checkState(0)
                is_orphan = stream_name not in self.global_waste_stream_names
                if state == Qt.Checked:
                    # Orphaned/deleted streams cannot be newly assigned or edited
                    # because there is no global stream definition to back them.
                    # Keeping them checked means preserve existing assignments.
                    if is_orphan:
                        unchanged_streams.append(stream_name)
                    else:
                        add_streams.append(stream_name)
                        update_stream_settings[stream_name] = self._settings_for_stream(
                            stream_name
                        )
                elif state == Qt.Unchecked:
                    remove_streams.append(stream_name)
                else:
                    unchanged_streams.append(stream_name)
        except Exception as exc:
            QMessageBox.critical(self, "Invalid waste stream settings", str(exc))
            return

        self.result = {
            "add_streams": add_streams,
            "remove_streams": remove_streams,
            "unchanged_streams": unchanged_streams,
            "update_stream_settings": update_stream_settings,
        }
        super().accept()


class TaskCategoryCommonLocationWizard(QDialog):
    """Assign one shared task-category location to a group of departments."""

    def __init__(
        self,
        parent,
        departments,
        location_names,
        task_generation_categories,
        preselected_indexes=None,
        current_floor=0,
        group_resolver=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Task category shared-location wizard")
        self.resize(920, 620)
        self.departments = [dict(x) for x in departments or []]
        self.location_names = sorted(
            {str(x).strip() for x in location_names if str(x).strip()}
        )
        self.task_generation_categories = list(task_generation_categories or [])
        self.preselected_indexes = set(preselected_indexes or [])
        self.current_floor = int(current_floor)
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.selected_locations = []
        self.result = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Assign the same pickup / drop-off location to several departments for a "
            "task generation category. This is useful where multiple departments share "
            "one store, bin, collection point or common drop-off location."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        layout.addLayout(form)

        self.category_combo = QComboBox()
        for category_key, category_label, _suffix in self.task_generation_categories:
            self.category_combo.addItem(str(category_label), str(category_key))
        form.addRow("Task category", self.category_combo)

        location_row = QHBoxLayout()
        self.location_summary = QLabel("None selected")
        self.location_summary.setWordWrap(True)
        pick_locations_btn = QPushButton("Select shared location...")
        pick_locations_btn.clicked.connect(self.pick_locations)
        clear_locations_btn = QPushButton("Clear")
        clear_locations_btn.clicked.connect(self.clear_locations)
        location_row.addWidget(self.location_summary, 1)
        location_row.addWidget(pick_locations_btn)
        location_row.addWidget(clear_locations_btn)
        form.addRow("Shared location(s)", location_row)

        options_row = QHBoxLayout()
        self.replace_existing_check = QCheckBox(
            "Replace existing assignments for this category"
        )
        self.replace_existing_check.setChecked(True)
        self.only_enabled_check = QCheckBox("Only enabled departments")
        self.only_enabled_check.setChecked(True)
        options_row.addWidget(self.replace_existing_check)
        options_row.addWidget(self.only_enabled_check)
        options_row.addStretch(1)
        form.addRow("Options", options_row)

        dept_tools = QHBoxLayout()
        layout.addLayout(dept_tools)
        select_all_btn = QPushButton("Select all")
        select_none_btn = QPushButton("Select none")
        select_floor_btn = QPushButton("Select current floor")
        select_existing_btn = QPushButton("Select departments already using category")
        select_all_btn.clicked.connect(self.select_all_departments)
        select_none_btn.clicked.connect(self.select_no_departments)
        select_floor_btn.clicked.connect(self.select_current_floor_departments)
        select_existing_btn.clicked.connect(self.select_departments_with_category)
        dept_tools.addWidget(select_all_btn)
        dept_tools.addWidget(select_none_btn)
        dept_tools.addWidget(select_floor_btn)
        dept_tools.addWidget(select_existing_btn)
        dept_tools.addStretch(1)

        self.department_table = QTreeWidget()
        self.department_table.setColumnCount(6)
        self.department_table.setHeaderLabels(
            ["Use", "ID", "Name", "Floor", "Enabled", "Current category locations"]
        )
        self.department_table.setRootIsDecorated(False)
        self.department_table.setAlternatingRowColors(True)
        self.department_table.header().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.department_table, 1)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.category_combo.currentIndexChanged.connect(self.refresh_department_table)
        self.refresh_department_table()
        self.refresh_location_summary()
        _polish_dialog(self)

    def _department_id(self, dept):
        return str(dept.get("id", "") or dept.get("name", "")).strip()

    def _category_key(self):
        return str(self.category_combo.currentData() or "").strip()

    def _category_locations_for_department(self, dept, category_key=None):
        category_key = str(category_key or self._category_key()).strip()
        if not category_key:
            return []
        category_locations = dept.get("task_generation_locations", {}) or {}
        if not isinstance(category_locations, dict):
            return []
        entry = category_locations.get(category_key, {})
        if isinstance(entry, dict):
            raw = entry.get("pickup_dropoff_locations", entry.get("locations", []))
        else:
            raw = entry
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in (raw or []) if str(x).strip()]

    def _department_label_group(self, dept):
        try:
            floor = int(dept.get("floor", 0))
            return f"Floor {floor}"
        except Exception:
            return "Other"

    def pick_locations(self):
        if not self.location_names:
            QMessageBox.information(
                self,
                "Shared location",
                "No locations are available. Create or place a location first.",
            )
            return

        picker = MultiSelectPicker(
            self,
            "Select shared category location(s)",
            self.location_names,
            selected=self.selected_locations,
            group_resolver=self.group_resolver,
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_locations = sorted(
                {str(x).strip() for x in picker.result if str(x).strip()}
            )
            self.refresh_location_summary()
            self.refresh_summary()

    def clear_locations(self):
        self.selected_locations = []
        self.refresh_location_summary()
        self.refresh_summary()

    def refresh_location_summary(self):
        if not self.selected_locations:
            self.location_summary.setText("None selected")
        elif len(self.selected_locations) <= 4:
            self.location_summary.setText(", ".join(self.selected_locations))
        else:
            self.location_summary.setText(f"{len(self.selected_locations)} selected")

    def refresh_department_table(self):
        previous_checked = self.checked_department_ids()
        self.department_table.clear()
        category_key = self._category_key()

        sorted_departments = sorted(
            enumerate(self.departments),
            key=lambda pair: (
                (
                    int(pair[1].get("floor", 0))
                    if str(pair[1].get("floor", "")).strip().lstrip("-").isdigit()
                    else 999999
                ),
                str(pair[1].get("name", "") or pair[1].get("id", "")).strip().lower(),
            ),
        )

        for index, dept in sorted_departments:
            dept_id = self._department_id(dept)
            if not dept_id:
                continue
            current_locations = self._category_locations_for_department(
                dept, category_key
            )
            item = QTreeWidgetItem(
                [
                    "",
                    dept_id,
                    str(dept.get("name", "")),
                    str(dept.get("floor", "")),
                    "Yes" if dept.get("enabled", True) else "No",
                    ", ".join(current_locations),
                ]
            )
            item.setData(0, Qt.UserRole, index)
            item.setData(1, Qt.UserRole, dept_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)

            checked = False
            if previous_checked:
                checked = dept_id in previous_checked
            elif self.preselected_indexes:
                checked = index in self.preselected_indexes
            item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            self.department_table.addTopLevelItem(item)

        for col in range(self.department_table.columnCount()):
            self.department_table.resizeColumnToContents(col)
        self.refresh_summary()

    def checked_department_ids(self):
        ids = []
        for row in range(self.department_table.topLevelItemCount()):
            item = self.department_table.topLevelItem(row)
            if item.checkState(0) != Qt.Checked:
                continue
            dept_id = str(item.data(1, Qt.UserRole) or "").strip()
            if dept_id:
                ids.append(dept_id)
        return ids

    def _set_department_checked_by_predicate(self, predicate):
        for row in range(self.department_table.topLevelItemCount()):
            item = self.department_table.topLevelItem(row)
            index = item.data(0, Qt.UserRole)
            try:
                dept = self.departments[int(index)]
            except Exception:
                dept = {}
            item.setCheckState(0, Qt.Checked if predicate(dept) else Qt.Unchecked)
        self.refresh_summary()

    def select_all_departments(self):
        self._set_department_checked_by_predicate(
            lambda dept: (not self.only_enabled_check.isChecked())
            or bool(dept.get("enabled", True))
        )

    def select_no_departments(self):
        self._set_department_checked_by_predicate(lambda _dept: False)

    def select_current_floor_departments(self):
        self._set_department_checked_by_predicate(
            lambda dept: (
                (
                    (not self.only_enabled_check.isChecked())
                    or bool(dept.get("enabled", True))
                )
                and int(dept.get("floor", -999999)) == self.current_floor
            )
        )

    def select_departments_with_category(self):
        category_key = self._category_key()
        self._set_department_checked_by_predicate(
            lambda dept: (
                (
                    (not self.only_enabled_check.isChecked())
                    or bool(dept.get("enabled", True))
                )
                and bool(self._category_locations_for_department(dept, category_key))
            )
        )

    def refresh_summary(self):
        checked = self.checked_department_ids()
        location_count = len(self.selected_locations)
        self.summary_label.setText(
            f"Ready to assign {location_count} shared location(s) to {len(checked)} department(s)."
        )

    def accept(self):
        try:
            category_key = self._category_key()
            if not category_key:
                raise ValueError("Select a task category")
            if not self.selected_locations:
                raise ValueError("Select at least one shared location")
            selected_ids = self.checked_department_ids()
            if not selected_ids:
                raise ValueError("Select at least one department")

            selected_set = set(selected_ids)
            assignments = {}
            for index, dept in enumerate(self.departments):
                dept_id = self._department_id(dept)
                if dept_id not in selected_set:
                    continue
                assignments[dept_id] = {
                    "department_index": index,
                    "locations": list(self.selected_locations),
                }

            self.result = {
                "category_key": category_key,
                "locations": list(self.selected_locations),
                "department_ids": selected_ids,
                "replace_existing": self.replace_existing_check.isChecked(),
                "assignments": assignments,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid shared-location assignment", str(exc))


class TaskCategorySharedBinGroupWizard(QDialog):
    """Assign waste-stream shared bin groups from departments that share category locations."""

    def __init__(
        self,
        parent,
        departments,
        waste_stream_names,
        task_generation_categories,
        preselected_indexes=None,
        current_floor=0,
    ):
        super().__init__(parent)
        self.setWindowTitle("Auto shared bin groups from category locations")
        self.resize(1040, 680)
        self.departments = [dict(x) for x in departments or []]
        self.preselected_indexes = set(preselected_indexes or [])
        self.current_floor = int(current_floor)
        self.task_generation_categories = list(task_generation_categories or [])
        self.waste_stream_names = sorted(
            {str(x).strip() for x in (waste_stream_names or []) if str(x).strip()}
            | self._assigned_waste_stream_names()
        )
        self.selected_waste_streams = list(self.waste_stream_names)
        self.result = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Find departments that already share the same assigned task-category "
            "location, then write a matching shared bin group onto their assigned "
            "waste streams. This does not change the existing category location "
            "assignments."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        layout.addLayout(form)

        self.category_combo = QComboBox()
        for category_key, category_label, _suffix in self.task_generation_categories:
            self.category_combo.addItem(str(category_label), str(category_key))
        # Waste is normally the relevant category. Select it by default when available.
        for idx in range(self.category_combo.count()):
            if str(self.category_combo.itemData(idx)).strip().lower() == "waste":
                self.category_combo.setCurrentIndex(idx)
                break
        form.addRow("Source task category", self.category_combo)

        stream_row = QHBoxLayout()
        self.stream_summary = QLabel("All assigned waste streams")
        self.stream_summary.setWordWrap(True)
        pick_streams_btn = QPushButton("Select streams...")
        pick_streams_btn.clicked.connect(self.pick_waste_streams)
        all_streams_btn = QPushButton("All")
        all_streams_btn.clicked.connect(self.select_all_waste_streams)
        stream_row.addWidget(self.stream_summary, 1)
        stream_row.addWidget(pick_streams_btn)
        stream_row.addWidget(all_streams_btn)
        form.addRow("Waste streams to update", stream_row)

        self.group_prefix_edit = QLineEdit("SHARED-BIN")
        form.addRow("Shared bin group prefix", self.group_prefix_edit)

        options_row = QHBoxLayout()
        self.only_enabled_check = QCheckBox("Only enabled departments")
        self.only_enabled_check.setChecked(True)
        self.only_selected_check = QCheckBox("Only selected departments")
        self.only_selected_check.setChecked(bool(self.preselected_indexes))
        self.overwrite_existing_check = QCheckBox(
            "Overwrite existing shared bin groups"
        )
        self.overwrite_existing_check.setChecked(True)
        self.minimum_group_size_edit = QLineEdit("2")
        self.minimum_group_size_edit.setFixedWidth(48)
        options_row.addWidget(self.only_enabled_check)
        options_row.addWidget(self.only_selected_check)
        options_row.addWidget(self.overwrite_existing_check)
        options_row.addWidget(QLabel("Minimum departments per group"))
        options_row.addWidget(self.minimum_group_size_edit)
        options_row.addStretch(1)
        form.addRow("Options", options_row)

        tool_row = QHBoxLayout()
        layout.addLayout(tool_row)
        refresh_btn = QPushButton("Refresh preview")
        select_all_btn = QPushButton("Select all groups")
        select_none_btn = QPushButton("Select none")
        current_floor_btn = QPushButton("Select current floor groups")
        refresh_btn.clicked.connect(self.refresh_preview)
        select_all_btn.clicked.connect(
            lambda: self._set_group_checks(lambda _group: True)
        )
        select_none_btn.clicked.connect(
            lambda: self._set_group_checks(lambda _group: False)
        )
        current_floor_btn.clicked.connect(self.select_current_floor_groups)
        tool_row.addWidget(refresh_btn)
        tool_row.addWidget(select_all_btn)
        tool_row.addWidget(select_none_btn)
        tool_row.addWidget(current_floor_btn)
        tool_row.addStretch(1)

        self.preview = QTreeWidget()
        self.preview.setColumnCount(7)
        self.preview.setHeaderLabels(
            [
                "Use",
                "Shared location",
                "Shared bin group",
                "Departments",
                "Floors",
                "Streams updated",
                "Skipped existing",
            ]
        )
        self.preview.setRootIsDecorated(False)
        self.preview.setAlternatingRowColors(True)
        self.preview.header().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.preview, 1)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.category_combo.currentIndexChanged.connect(self.refresh_preview)
        self.only_enabled_check.toggled.connect(self.refresh_preview)
        self.only_selected_check.toggled.connect(self.refresh_preview)
        self.overwrite_existing_check.toggled.connect(self.refresh_preview)
        self.group_prefix_edit.textChanged.connect(self.refresh_preview)
        self.minimum_group_size_edit.textChanged.connect(self.refresh_preview)

        self.refresh_stream_summary()
        self.refresh_preview()
        _polish_dialog(self)

    def _department_id(self, dept):
        return str(dept.get("id", "") or dept.get("name", "")).strip()

    def _category_key(self):
        return str(self.category_combo.currentData() or "").strip()

    def _assigned_waste_stream_names(self):
        names = set()
        for dept in self.departments:
            for item in dept.get("waste_streams", []) or []:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                else:
                    name = str(item).strip()
                if name:
                    names.add(name)
        return names

    def _normalise_stream_item(self, item):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                return None
            result = dict(item)
            result["name"] = name
            result["shared_container_group"] = str(
                result.get(
                    "shared_container_group", result.get("shared_container_id", "")
                )
                or ""
            ).strip()
            result["shared_container"] = bool(
                result.get("shared_container", bool(result["shared_container_group"]))
            )
            return result

        name = str(item).strip()
        if not name:
            return None
        return {
            "name": name,
            "generation_mode": "threshold",
            "frequency_per_day": 0.0,
            "volume_per_event_m3": 0.0,
            "threshold_volume_m3": 0.0,
            "base_daily_volume_m3": 0.0,
            "scheduled_times": [],
            "initial_container_present": True,
            "shared_container": False,
            "shared_container_group": "",
        }

    def _department_streams_by_name(self, dept):
        result = {}
        for item in dept.get("waste_streams", []) or []:
            normalised = self._normalise_stream_item(item)
            if normalised:
                result[normalised["name"]] = normalised
        return result

    def _category_locations_for_department(self, dept, category_key=None):
        category_key = str(category_key or self._category_key()).strip()
        category_locations = dept.get("task_generation_locations", {}) or {}
        if not category_key or not isinstance(category_locations, dict):
            return []

        entry = category_locations.get(category_key, {})
        if isinstance(entry, dict):
            raw = entry.get("pickup_dropoff_locations", entry.get("locations", []))
        else:
            raw = entry
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in (raw or []) if str(x).strip()]

    def _safe_group_suffix(self, value):
        text = str(value or "").strip().upper()
        text = "".join(ch if ch.isalnum() else "-" for ch in text)
        while "--" in text:
            text = text.replace("--", "-")
        return text.strip("-") or "GROUP"

    def _group_id_for_location(self, location_name):
        prefix = self.group_prefix_edit.text().strip() or "SHARED-BIN"
        return f"{prefix}-{self._safe_group_suffix(location_name)}"

    def pick_waste_streams(self):
        if not self.waste_stream_names:
            QMessageBox.information(
                self,
                "Waste streams",
                "No waste streams are available on the selected departments.",
            )
            return

        picker = MultiSelectPicker(
            self,
            "Select waste streams to update",
            self.waste_stream_names,
            selected=self.selected_waste_streams,
            group_resolver=lambda _item: "Waste streams",
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_waste_streams = sorted(
                {str(x).strip() for x in picker.result if str(x).strip()}
            )
            self.refresh_stream_summary()
            self.refresh_preview()

    def select_all_waste_streams(self):
        self.selected_waste_streams = list(self.waste_stream_names)
        self.refresh_stream_summary()
        self.refresh_preview()

    def refresh_stream_summary(self):
        if not self.selected_waste_streams:
            self.stream_summary.setText("No streams selected")
        elif len(self.selected_waste_streams) == len(self.waste_stream_names):
            self.stream_summary.setText("All assigned waste streams")
        elif len(self.selected_waste_streams) <= 5:
            self.stream_summary.setText(", ".join(self.selected_waste_streams))
        else:
            self.stream_summary.setText(
                f"{len(self.selected_waste_streams)} streams selected"
            )

    def _minimum_group_size(self):
        try:
            return max(2, int(float(self.minimum_group_size_edit.text() or 2)))
        except Exception:
            return 2

    def _candidate_department_indexes(self):
        selected_filter = set(self.preselected_indexes)
        only_selected = self.only_selected_check.isChecked() and bool(selected_filter)
        indexes = []
        for index, dept in enumerate(self.departments):
            if only_selected and index not in selected_filter:
                continue
            if self.only_enabled_check.isChecked() and not bool(
                dept.get("enabled", True)
            ):
                continue
            if not self._department_id(dept):
                continue
            indexes.append(index)
        return indexes

    def _build_location_groups(self):
        category_key = self._category_key()
        grouped = {}
        for index in self._candidate_department_indexes():
            dept = self.departments[index]
            for location_name in self._category_locations_for_department(
                dept, category_key
            ):
                grouped.setdefault(location_name, []).append(index)

        minimum_group_size = self._minimum_group_size()
        groups = []
        for location_name, indexes in sorted(
            grouped.items(), key=lambda pair: pair[0].lower()
        ):
            unique_indexes = []
            seen = set()
            for idx in indexes:
                if idx not in seen:
                    seen.add(idx)
                    unique_indexes.append(idx)
            if len(unique_indexes) < minimum_group_size:
                continue

            stream_update_count = 0
            skipped_existing_count = 0
            missing_stream_count = 0
            selected_streams = set(self.selected_waste_streams)
            overwrite = self.overwrite_existing_check.isChecked()

            for idx in unique_indexes:
                streams_by_name = self._department_streams_by_name(
                    self.departments[idx]
                )
                for stream_name in selected_streams:
                    stream = streams_by_name.get(stream_name)
                    if not stream:
                        missing_stream_count += 1
                        continue
                    existing_group = str(
                        stream.get("shared_container_group", "")
                    ).strip()
                    if existing_group and not overwrite:
                        skipped_existing_count += 1
                    else:
                        stream_update_count += 1

            groups.append(
                {
                    "location": location_name,
                    "group_id": self._group_id_for_location(location_name),
                    "department_indexes": unique_indexes,
                    "stream_update_count": stream_update_count,
                    "skipped_existing_count": skipped_existing_count,
                    "missing_stream_count": missing_stream_count,
                }
            )
        return groups

    def refresh_preview(self):
        previous_checked = set()
        for row in range(self.preview.topLevelItemCount()):
            item = self.preview.topLevelItem(row)
            if item.checkState(0) == Qt.Checked:
                previous_checked.add(str(item.data(1, Qt.UserRole) or ""))

        self.preview.clear()
        groups = self._build_location_groups()
        had_previous = bool(previous_checked)

        for group in groups:
            dept_names = []
            floors = set()
            for idx in group["department_indexes"]:
                dept = self.departments[idx]
                dept_names.append(
                    str(dept.get("name", "")).strip()
                    or str(dept.get("id", "")).strip()
                    or f"Department {idx + 1}"
                )
                floors.add(str(dept.get("floor", "")))

            item = QTreeWidgetItem(
                [
                    "",
                    group["location"],
                    group["group_id"],
                    ", ".join(dept_names[:6])
                    + (f", +{len(dept_names) - 6}" if len(dept_names) > 6 else ""),
                    ", ".join(sorted(floors)),
                    str(group["stream_update_count"]),
                    str(group["skipped_existing_count"]),
                ]
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(1, Qt.UserRole, group["location"])
            item.setData(2, Qt.UserRole, group)
            checked = group["location"] in previous_checked if had_previous else True
            item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            self.preview.addTopLevelItem(item)

        for col in range(self.preview.columnCount()):
            self.preview.resizeColumnToContents(col)
        self.refresh_summary()

    def _checked_groups(self):
        groups = []
        for row in range(self.preview.topLevelItemCount()):
            item = self.preview.topLevelItem(row)
            if item.checkState(0) != Qt.Checked:
                continue
            group = item.data(2, Qt.UserRole)
            if isinstance(group, dict):
                groups.append(group)
        return groups

    def _set_group_checks(self, predicate):
        for row in range(self.preview.topLevelItemCount()):
            item = self.preview.topLevelItem(row)
            group = item.data(2, Qt.UserRole)
            item.setCheckState(0, Qt.Checked if predicate(group) else Qt.Unchecked)
        self.refresh_summary()

    def select_current_floor_groups(self):
        current_floor = str(self.current_floor)
        self._set_group_checks(
            lambda group: any(
                str(self.departments[idx].get("floor", "")) == current_floor
                for idx in (group or {}).get("department_indexes", [])
            )
        )

    def refresh_summary(self):
        checked_groups = self._checked_groups()
        dept_ids = set()
        stream_updates = 0
        skipped = 0
        for group in checked_groups:
            stream_updates += int(group.get("stream_update_count", 0) or 0)
            skipped += int(group.get("skipped_existing_count", 0) or 0)
            for idx in group.get("department_indexes", []):
                dept_ids.add(self._department_id(self.departments[idx]))
        self.summary_label.setText(
            f"Selected {len(checked_groups)} shared-location group(s), "
            f"covering {len(dept_ids)} department(s), with {stream_updates} waste stream update(s)"
            + (f" and {skipped} existing group(s) skipped." if skipped else ".")
        )

    def accept(self):
        try:
            category_key = self._category_key()
            if not category_key:
                raise ValueError("Select a source task category")
            if not self.selected_waste_streams:
                raise ValueError("Select at least one waste stream")
            groups = self._checked_groups()
            if not groups:
                raise ValueError("Select at least one shared-location group")

            self.result = {
                "category_key": category_key,
                "waste_streams": list(self.selected_waste_streams),
                "overwrite_existing": self.overwrite_existing_check.isChecked(),
                "groups": groups,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid shared bin group assignment", str(exc))


class DepartmentListDialog(QDialog):

    def __init__(
        self,
        parent,
        items,
        location_names,
        waste_stream_names,
        current_floor,
        on_save,
        suggest_department_id,
        group_resolver=None,
        task_generation_categories=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Departments")
        self.resize(980, 520)
        self.items = [dict(x) for x in items]
        self.location_names = sorted(location_names)
        self.waste_stream_names = sorted(waste_stream_names)
        self.current_floor = int(current_floor)
        self.on_save = on_save
        self.suggest_department_id = suggest_department_id
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.task_generation_categories = list(task_generation_categories or [])

        layout = QVBoxLayout(self)

        self.table = QTreeWidget()
        self.table.setColumnCount(10)
        self.table.setHeaderLabels(
            [
                "ID",
                "Name",
                "Floor",
                "Enabled",
                "Beds",
                "Turnover",
                "Staff",
                "Operating hours",
                "Days",
                "Waste streams",
            ]
        )
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemDoubleClicked.connect(lambda _item, _col: self.edit_item())
        self.table.header().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        layout.addLayout(row)

        add_btn = QPushButton("Add")
        edit_btn = QPushButton("Edit")
        del_btn = QPushButton("Delete")
        save_btn = QPushButton("Save")

        auto_assign_btn = QPushButton("Auto assign locations")
        category_wizard_btn = QPushButton("Task category wizard...")
        shared_bin_groups_btn = QPushButton("Auto shared bin groups...")
        bulk_waste_btn = QPushButton("Manage waste streams...")
        export_csv_btn = QPushButton("Export CSV")

        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(del_btn)
        row.addWidget(auto_assign_btn)
        row.addWidget(category_wizard_btn)
        row.addWidget(shared_bin_groups_btn)
        row.addWidget(bulk_waste_btn)
        row.addWidget(export_csv_btn)
        row.addStretch(1)
        row.addWidget(save_btn)

        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        del_btn.clicked.connect(self.delete_item)
        auto_assign_btn.clicked.connect(self.auto_assign_locations)
        category_wizard_btn.clicked.connect(self.open_task_category_wizard)
        shared_bin_groups_btn.clicked.connect(self.open_shared_bin_group_wizard)
        bulk_waste_btn.clicked.connect(
            self.manage_waste_streams_for_selected_departments
        )
        export_csv_btn.clicked.connect(self.export_departments_csv)
        save_btn.clicked.connect(self.save_items)

        self._refresh_table()
        _polish_dialog(self)

    def _refresh_table(self):
        self.table.clear()
        self._tree_item_to_index = {}

        grouped = {}

        for idx, item in enumerate(self.items):
            try:
                floor = int(item.get("floor", 0))
            except Exception:
                floor = 0
            grouped.setdefault(floor, []).append((idx, item))

        for floor in sorted(grouped.keys()):
            floor_item = QTreeWidgetItem(
                [f"Floor {floor}", "", "", "", "", "", "", "", "", ""]
            )
            floor_item.setFirstColumnSpanned(True)
            floor_item.setExpanded(True)
            self.table.addTopLevelItem(floor_item)

            grouped[floor].sort(
                key=lambda pair: (
                    str(pair[1].get("name", "")).strip().lower()
                    or str(pair[1].get("id", "")).strip().lower()
                )
            )

            for idx, item in grouped[floor]:
                waste_labels = []
                for stream in item.get("waste_streams", []):
                    if isinstance(stream, dict):
                        stream_name = str(stream.get("name", "")).strip()
                        shared_group = str(
                            stream.get(
                                "shared_container_group",
                                stream.get("shared_container_id", ""),
                            )
                            or ""
                        ).strip()
                        if stream_name and shared_group:
                            waste_labels.append(f"{stream_name} [{shared_group}]")
                        elif stream_name:
                            waste_labels.append(stream_name)
                    else:
                        stream_name = str(stream).strip()
                        if stream_name:
                            waste_labels.append(stream_name)
                waste_text = ", ".join(waste_labels)

                operating_start = str(
                    item.get("operating_start_time", "00:00") or "00:00"
                )
                operating_end = str(item.get("operating_end_time", "") or "")
                if operating_end:
                    operating_text = f"{operating_start}-{operating_end}"
                else:
                    operating_text = (
                        f"{operating_start} + {item.get('hours_operated_per_day', 24)}h"
                    )
                days_text = ", ".join(item.get("days_active", []))

                child = QTreeWidgetItem(
                    [
                        str(item.get("id", "")),
                        str(item.get("name", "")),
                        str(item.get("floor", "")),
                        "Yes" if item.get("enabled", True) else "No",
                        str(item.get("bed_count", 0)),
                        str(item.get("patient_turnover", 0.0)),
                        str(item.get("staff_count", 0)),
                        operating_text,
                        days_text,
                        waste_text,
                    ]
                )

                child.setData(0, Qt.UserRole, idx)
                floor_item.addChild(child)
                self._tree_item_to_index[id(child)] = idx

        for col in range(self.table.columnCount()):
            self.table.resizeColumnToContents(col)

    def _selected_department_indexes(self):
        indexes = []

        for item in self.table.selectedItems():
            idx = item.data(0, Qt.UserRole)
            if idx is None:
                continue
            try:
                indexes.append(int(idx))
            except Exception:
                continue

        return sorted(set(indexes))

    def open_task_category_wizard(self):
        if not self.task_generation_categories:
            QMessageBox.information(
                self,
                "Task category wizard",
                "No task generation categories are available.",
            )
            return

        selected_indexes = self._selected_department_indexes()

        dialog = TaskCategoryCommonLocationWizard(
            self,
            departments=self.items,
            location_names=self.location_names,
            task_generation_categories=self.task_generation_categories,
            preselected_indexes=selected_indexes,
            current_floor=self.current_floor,
            group_resolver=self.group_resolver,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return

        category_key = str(dialog.result.get("category_key", "")).strip()
        shared_locations = [
            str(x).strip() for x in dialog.result.get("locations", []) if str(x).strip()
        ]
        replace_existing = bool(dialog.result.get("replace_existing", True))
        assignments = dialog.result.get("assignments", {}) or {}

        if not category_key or not shared_locations or not assignments:
            return

        updated_count = 0
        location_reference_count = 0

        for assignment in assignments.values():
            try:
                index = int(assignment.get("department_index"))
            except Exception:
                continue
            if index < 0 or index >= len(self.items):
                continue

            dept = self.items[index]
            category_locations = dept.setdefault("task_generation_locations", {})
            if not isinstance(category_locations, dict):
                category_locations = {}
                dept["task_generation_locations"] = category_locations

            entry = category_locations.setdefault(category_key, {})
            if not isinstance(entry, dict):
                entry = {"pickup_dropoff_locations": list(entry or [])}
                category_locations[category_key] = entry

            if replace_existing:
                selected = []
            else:
                existing = entry.get(
                    "pickup_dropoff_locations", entry.get("locations", [])
                )
                if isinstance(existing, str):
                    selected = [existing]
                elif isinstance(existing, list):
                    selected = list(existing)
                else:
                    selected = list(existing or [])

            seen = {str(x).strip() for x in selected if str(x).strip()}
            for location_name in shared_locations:
                if location_name not in seen:
                    selected.append(location_name)
                    seen.add(location_name)
                    location_reference_count += 1

            entry["pickup_dropoff_locations"] = [
                str(x).strip() for x in selected if str(x).strip()
            ]
            entry["locations"] = list(entry["pickup_dropoff_locations"])
            updated_count += 1

        for location_name in shared_locations:
            if location_name not in self.location_names:
                self.location_names.append(location_name)
        self.location_names = sorted(set(self.location_names))

        self._refresh_table()

        QMessageBox.information(
            self,
            "Task category wizard",
            (
                f"Updated {updated_count} department(s).\n"
                f"Assigned {location_reference_count} new shared location reference(s)."
            ),
        )

    def open_shared_bin_group_wizard(self):
        if not self.task_generation_categories:
            QMessageBox.information(
                self,
                "Auto shared bin groups",
                "No task generation categories are available.",
            )
            return

        selected_indexes = self._selected_department_indexes()

        dialog = TaskCategorySharedBinGroupWizard(
            self,
            departments=self.items,
            waste_stream_names=self.waste_stream_names,
            task_generation_categories=self.task_generation_categories,
            preselected_indexes=selected_indexes,
            current_floor=self.current_floor,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return

        selected_streams = {
            str(x).strip()
            for x in dialog.result.get("waste_streams", [])
            if str(x).strip()
        }
        overwrite_existing = bool(dialog.result.get("overwrite_existing", True))
        groups = list(dialog.result.get("groups", []) or [])

        if not selected_streams or not groups:
            return

        updated_count = 0
        skipped_existing_count = 0
        missing_stream_count = 0
        departments_touched = set()

        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            if not group_id:
                continue

            for index in group.get("department_indexes", []) or []:
                try:
                    index = int(index)
                except Exception:
                    continue
                if index < 0 or index >= len(self.items):
                    continue

                dept = self.items[index]
                streams = []
                streams_by_name = {}

                for item in dept.get("waste_streams", []) or []:
                    normalised = self._normalise_waste_stream_for_shared_group(item)
                    if not normalised:
                        continue
                    streams.append(normalised)
                    streams_by_name[normalised["name"]] = normalised

                for stream_name in selected_streams:
                    stream = streams_by_name.get(stream_name)
                    if stream is None:
                        missing_stream_count += 1
                        continue

                    existing_group = str(
                        stream.get(
                            "shared_container_group",
                            stream.get("shared_container_id", ""),
                        )
                        or ""
                    ).strip()

                    if existing_group and not overwrite_existing:
                        skipped_existing_count += 1
                        continue

                    if existing_group != group_id or not stream.get("shared_container"):
                        updated_count += 1

                    stream["shared_container"] = True
                    stream["shared_container_group"] = group_id
                    stream.pop("shared_container_id", None)
                    departments_touched.add(index)

                dept["waste_streams"] = streams

        self._refresh_table()

        QMessageBox.information(
            self,
            "Auto shared bin groups",
            (
                f"Updated {updated_count} waste stream assignment(s) "
                f"across {len(departments_touched)} department(s).\n"
                f"Skipped {skipped_existing_count} existing shared bin group(s).\n"
                f"Skipped {missing_stream_count} department/stream combination(s) where the selected stream was not assigned."
            ),
        )

    def _normalise_waste_stream_for_shared_group(self, item):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                return None
            normalised = dict(item)
            normalised["name"] = name
            normalised.setdefault("generation_mode", "threshold")
            normalised["frequency_per_day"] = float(
                normalised.get("frequency_per_day", 0.0) or 0.0
            )
            normalised["volume_per_event_m3"] = float(
                normalised.get("volume_per_event_m3", 0.0) or 0.0
            )
            normalised["threshold_volume_m3"] = float(
                normalised.get("threshold_volume_m3", 0.0) or 0.0
            )
            normalised["base_daily_volume_m3"] = float(
                normalised.get("base_daily_volume_m3", 0.0) or 0.0
            )
            normalised["scheduled_times"] = list(
                normalised.get("scheduled_times", []) or []
            )
            normalised["initial_container_present"] = bool(
                normalised.get("initial_container_present", True)
            )
            normalised["shared_container_group"] = str(
                normalised.get(
                    "shared_container_group",
                    normalised.get("shared_container_id", ""),
                )
                or ""
            ).strip()
            normalised["shared_container"] = bool(
                normalised.get(
                    "shared_container", bool(normalised["shared_container_group"])
                )
            )
            return normalised

        name = str(item).strip()
        if not name:
            return None
        return {
            "name": name,
            "generation_mode": "threshold",
            "frequency_per_day": 0.0,
            "volume_per_event_m3": 0.0,
            "threshold_volume_m3": 0.0,
            "base_daily_volume_m3": 0.0,
            "scheduled_times": [],
            "initial_container_present": True,
            "shared_container": False,
            "shared_container_group": "",
        }

    def auto_assign_locations(self):
        if not self.task_generation_categories:
            QMessageBox.information(
                self,
                "Auto assign locations",
                "No task generation categories are available.",
            )
            return

        location_set = {str(x).strip() for x in self.location_names if str(x).strip()}
        assigned_count = 0
        kept_count = 0

        for dept in self.items:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                continue

            suffixes = dept.setdefault("task_generation_location_suffixes", {})
            category_locations = dept.setdefault("task_generation_locations", {})

            for (
                category_key,
                _category_label,
                default_suffix,
            ) in self.task_generation_categories:
                suffix = str(suffixes.get(category_key, default_suffix)).strip()
                expected_prefix = f"{dept_id}{suffix}"

                matching_locations = sorted(
                    name
                    for name in location_set
                    if name == expected_prefix or name.startswith(f"{expected_prefix}_")
                )

                if not matching_locations:
                    continue

                entry = category_locations.setdefault(category_key, {})
                selected = entry.setdefault("pickup_dropoff_locations", [])

                existing = {str(x).strip() for x in selected if str(x).strip()}

                kept_count += len(existing)

                for location_name in matching_locations:
                    if location_name not in existing:
                        selected.append(location_name)
                        existing.add(location_name)
                        assigned_count += 1

        self._refresh_table()

        QMessageBox.information(
            self,
            "Auto assign locations",
            (
                f"Assigned {assigned_count} location reference(s).\n"
                f"Kept {kept_count} existing assigned location reference(s)."
            ),
        )

    def _normalise_department_waste_streams(self, value):
        result = []
        for item in value or []:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                result.append(
                    {
                        "name": name,
                        "generation_mode": str(
                            item.get("generation_mode", "threshold")
                        ),
                        "frequency_per_day": float(
                            item.get("frequency_per_day", 0.0) or 0.0
                        ),
                        "volume_per_event_m3": float(
                            item.get("volume_per_event_m3", 0.0) or 0.0
                        ),
                        "threshold_volume_m3": float(
                            item.get("threshold_volume_m3", 0.0) or 0.0
                        ),
                        "base_daily_volume_m3": float(
                            item.get("base_daily_volume_m3", 0.0) or 0.0
                        ),
                        "scheduled_times": list(item.get("scheduled_times", []) or []),
                        "initial_container_present": bool(
                            item.get("initial_container_present", True)
                        ),
                        "shared_container": bool(item.get("shared_container", False)),
                        "shared_container_group": str(
                            item.get(
                                "shared_container_group",
                                item.get("shared_container_id", ""),
                            )
                        ).strip(),
                    }
                )
                continue

            name = str(item).strip()
            if name:
                result.append(
                    {
                        "name": name,
                        "generation_mode": "threshold",
                        "frequency_per_day": 0.0,
                        "volume_per_event_m3": 0.0,
                        "threshold_volume_m3": 0.0,
                        "base_daily_volume_m3": 0.0,
                        "scheduled_times": [],
                        "initial_container_present": True,
                        "shared_container": False,
                        "shared_container_group": "",
                    }
                )

        # De-duplicate while preserving the first existing settings for each stream.
        seen = set()
        clean = []
        for item in result:
            name = str(item.get("name", "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            clean.append(item)
        return clean

    def manage_waste_streams_for_selected_departments(self):
        rows = self._selected_department_indexes()

        if not rows:
            QMessageBox.information(
                self,
                "Manage waste streams",
                "Select one or more departments first.",
            )
            return

        selected_departments = [
            self.items[row] for row in rows if 0 <= row < len(self.items)
        ]

        if not selected_departments:
            return

        selected_assigned_streams = set()
        for dept in selected_departments:
            for item in dept.get("waste_streams", []) or []:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                else:
                    name = str(item).strip()
                if name:
                    selected_assigned_streams.add(name)

        if not self.waste_stream_names and not selected_assigned_streams:
            QMessageBox.information(
                self,
                "Manage waste streams",
                "No global or currently assigned waste streams are available.",
            )
            return

        dialog = BulkDepartmentWasteStreamControlDialog(
            self,
            waste_stream_names=self.waste_stream_names,
            departments=selected_departments,
        )

        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return

        add_streams = {
            str(x).strip()
            for x in dialog.result.get("add_streams", [])
            if str(x).strip()
        }
        remove_streams = {
            str(x).strip()
            for x in dialog.result.get("remove_streams", [])
            if str(x).strip()
        }
        update_stream_settings = {
            str(name).strip(): dict(settings or {})
            for name, settings in (
                dialog.result.get("update_stream_settings", {}) or {}
            ).items()
            if str(name).strip()
        }

        if not add_streams and not remove_streams and not update_stream_settings:
            return

        added_count = 0
        removed_count = 0
        updated_count = 0

        for row in rows:
            if row < 0 or row >= len(self.items):
                continue

            dept = self.items[row]
            existing_items = self._normalise_department_waste_streams(
                dept.get("waste_streams", [])
            )

            filtered_items = []
            existing_names = set()

            for item in existing_items:
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                if name in remove_streams:
                    removed_count += 1
                    continue

                if name in update_stream_settings:
                    updated_item = dict(item)
                    updated_item.update(update_stream_settings[name])
                    updated_item["name"] = name
                    filtered_items.append(updated_item)
                    updated_count += 1
                else:
                    filtered_items.append(item)

                existing_names.add(name)

            for stream_name in sorted(add_streams):
                if stream_name in existing_names:
                    continue
                settings = dict(
                    update_stream_settings.get(
                        stream_name,
                        BulkDepartmentWasteStreamControlDialog.DEFAULT_STREAM_SETTINGS,
                    )
                )
                filtered_items.append({"name": stream_name, **settings})
                existing_names.add(stream_name)
                added_count += 1
                updated_count += 1

            dept["waste_streams"] = filtered_items

        self._refresh_table()

        QMessageBox.information(
            self,
            "Manage waste streams",
            (
                f"Updated {len(rows)} selected department(s).\n"
                f"Added {added_count} waste stream assignment(s).\n"
                f"Removed {removed_count} waste stream assignment(s).\n"
                f"Applied generation settings to {updated_count} department stream assignment(s)."
            ),
        )

    def _find_department_index_by_id(self, department_id):
        department_id = str(department_id or "").strip()
        if not department_id:
            return None
        for idx, item in enumerate(self.items):
            current_id = str(item.get("id", "") or "").strip()
            if current_id == department_id:
                return idx
        # Placement can happen after the user edits the ID in the open editor;
        # in that case use the visible department name as a fallback where possible.
        for idx, item in enumerate(self.items):
            current_name = str(item.get("name", "") or "").strip()
            if current_name and current_name == department_id:
                return idx
        return None

    def register_placed_department_location(
        self, department_id, category_key, location_payload
    ):
        """Immediately record a placed category location against the department list model.

        The graphical placement callback occurs while DepartmentEditorDialog is hidden.
        Updating self.items here means reopening the editor before pressing the list
        Save button still shows the selected location.
        """
        department_id = str(department_id or "").strip()
        category_key = str(category_key or "").strip()
        location_name = str((location_payload or {}).get("name", "") or "").strip()
        if not department_id or not category_key or not location_name:
            return

        idx = self._find_department_index_by_id(department_id)
        if idx is None:
            return

        dept = self.items[idx]
        category_locations = dept.setdefault("task_generation_locations", {})
        entry = category_locations.setdefault(category_key, {})
        if not isinstance(entry, dict):
            entry = {"pickup_dropoff_locations": list(entry or [])}
            category_locations[category_key] = entry

        selected = entry.setdefault(
            "pickup_dropoff_locations",
            entry.get("locations", []),
        )
        if not isinstance(selected, list):
            selected = [selected] if selected else []
            entry["pickup_dropoff_locations"] = selected

        existing = {str(x).strip() for x in selected if str(x).strip()}
        if location_name not in existing:
            selected.append(location_name)

        # Keep legacy key in step for code that still reads locations.
        entry["locations"] = list(entry.get("pickup_dropoff_locations", []))

        if location_name not in self.location_names:
            self.location_names.append(location_name)
            self.location_names = sorted(set(self.location_names))

        # Keep a pending create payload on the department item as a fallback.
        pending = dept.setdefault("_create_locations", [])
        if isinstance(pending, list):
            if not any(
                str(x.get("name", "") or "").strip() == location_name
                for x in pending
                if isinstance(x, dict)
            ):
                pending.append(dict(location_payload or {}))

        self._refresh_table()

    def _merge_existing_category_locations_into_result(self, result):
        """Do not allow a stale editor result to erase direct placement registrations."""
        result = dict(result or {})
        idx = self._find_department_index_by_id(result.get("id", ""))
        if idx is None:
            return result

        existing_locations = self.items[idx].get("task_generation_locations", {}) or {}
        result_locations = result.setdefault("task_generation_locations", {})
        if not isinstance(result_locations, dict):
            result_locations = {}
            result["task_generation_locations"] = result_locations

        for category_key, existing_entry in existing_locations.items():
            if isinstance(existing_entry, dict):
                existing_raw = existing_entry.get(
                    "pickup_dropoff_locations",
                    existing_entry.get("locations", []),
                )
            else:
                existing_raw = existing_entry
            existing_names = [
                str(x).strip() for x in (existing_raw or []) if str(x).strip()
            ]
            if not existing_names:
                continue

            target_entry = result_locations.setdefault(str(category_key), {})
            if not isinstance(target_entry, dict):
                target_entry = {"pickup_dropoff_locations": list(target_entry or [])}
                result_locations[str(category_key)] = target_entry
            target = target_entry.setdefault(
                "pickup_dropoff_locations",
                target_entry.get("locations", []),
            )
            if not isinstance(target, list):
                target = [target] if target else []
                target_entry["pickup_dropoff_locations"] = target
            present = {str(x).strip() for x in target if str(x).strip()}
            for name in existing_names:
                if name not in present:
                    target.append(name)
                    present.add(name)
            target_entry["locations"] = list(
                target_entry.get("pickup_dropoff_locations", [])
            )

        return result

    def _commit_department_editor_result(self, result):
        result = self._merge_existing_category_locations_into_result(result)

        parent = self.parent()
        if parent is not None and hasattr(
            parent, "create_department_generated_locations"
        ):
            parent.create_department_generated_locations(result)
        else:
            result.pop("_create_locations", None)

        for category_entry in (
            result.get("task_generation_locations", {}) or {}
        ).values():
            if isinstance(category_entry, dict):
                raw_locations = category_entry.get(
                    "pickup_dropoff_locations", category_entry.get("locations", [])
                )
            else:
                raw_locations = category_entry

            for location_name in raw_locations or []:
                location_name = str(location_name or "").strip()
                if location_name and location_name not in self.location_names:
                    self.location_names.append(location_name)

        self.location_names = sorted(set(self.location_names))
        result.pop("_create_locations", None)
        return result

    def _category_location_text_for_export(self, dept, category_key):
        category_locations = dept.get("task_generation_locations", {}) or {}
        if not isinstance(category_locations, dict):
            return ""
        entry = category_locations.get(category_key, {})
        if isinstance(entry, dict):
            raw = entry.get("pickup_dropoff_locations", entry.get("locations", []))
        else:
            raw = entry
        if isinstance(raw, str):
            raw = [raw]
        return "; ".join(str(x).strip() for x in (raw or []) if str(x).strip())

    def _waste_streams_text_for_export(self, dept):
        labels = []
        for stream in dept.get("waste_streams", []) or []:
            if isinstance(stream, dict):
                name = str(stream.get("name", "") or "").strip()
                if not name:
                    continue
                mode = str(stream.get("generation_mode", "") or "").strip()
                group = str(
                    stream.get(
                        "shared_container_group",
                        stream.get("shared_container_id", ""),
                    )
                    or ""
                ).strip()
                parts = [name]
                if mode:
                    parts.append(f"mode={mode}")
                if group:
                    parts.append(f"shared={group}")
                labels.append(" [" + ", ".join(parts) + "]" if False else ", ".join(parts))
            else:
                name = str(stream or "").strip()
                if name:
                    labels.append(name)
        return "; ".join(labels)

    def _department_export_rows(self):
        category_keys = [
            str(category_key).strip()
            for category_key, *_ in self.task_generation_categories
            if str(category_key).strip()
        ]
        rows = []
        for dept in self.items:
            operating_start = str(dept.get("operating_start_time", "00:00") or "00:00")
            operating_end = str(dept.get("operating_end_time", "") or "")
            row = {
                "id": str(dept.get("id", "") or ""),
                "name": str(dept.get("name", "") or ""),
                "floor": dept.get("floor", ""),
                "enabled": "Yes" if dept.get("enabled", True) else "No",
                "bed_count": dept.get("bed_count", 0),
                "patient_turnover": dept.get("patient_turnover", 0.0),
                "staff_count": dept.get("staff_count", 0),
                "operating_start_time": operating_start,
                "operating_end_time": operating_end,
                "hours_operated_per_day": dept.get("hours_operated_per_day", ""),
                "days_active": "; ".join(str(x).strip() for x in dept.get("days_active", []) if str(x).strip()),
                "waste_streams": self._waste_streams_text_for_export(dept),
                "x": dept.get("x", ""),
                "y": dept.get("y", ""),
            }
            for category_key in category_keys:
                row[f"{category_key}_locations"] = self._category_location_text_for_export(dept, category_key)
            rows.append(row)
        return rows

    def export_departments_csv(self):
        if not self.items:
            QMessageBox.information(
                self,
                "Export departments",
                "There are no departments to export.",
            )
            return

        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export departments to CSV",
            "departments.csv",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return

        if not path.lower().endswith(".csv"):
            path += ".csv"

        try:
            rows = self._department_export_rows()
            fieldnames = []
            for row in rows:
                for key in row.keys():
                    if key not in fieldnames:
                        fieldnames.append(key)

            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            QMessageBox.information(
                self,
                "Export departments",
                f"Exported {len(rows)} department(s) to:\n{path}",
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Export departments",
                f"Could not export departments CSV:\n{exc}",
            )

    def _clean_department_items_for_save(self):
        cleaned = []
        for item in self.items:
            clean = dict(item)
            clean.pop("_create_locations", None)
            cleaned.append(clean)
        return cleaned

    def add_item(self):
        dialog = DepartmentEditorDialog(
            self,
            location_names=self.location_names,
            waste_stream_names=self.waste_stream_names,
            current_floor=self.current_floor,
            default_department_id=self.suggest_department_id(),
            group_resolver=self.group_resolver,
            task_generation_categories=self.task_generation_categories,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_id = str(dialog.result.get("id", "")).strip()
            new_name = str(dialog.result.get("name", "")).strip()

            for item in self.items:
                if str(item.get("id", "")).strip() == new_id:
                    QMessageBox.critical(
                        self, "Duplicate", "Department ID already exists"
                    )
                    return
                if str(item.get("name", "")).strip() == new_name:
                    QMessageBox.critical(
                        self, "Duplicate", "Department name already exists"
                    )
                    return

            self.items.append(self._commit_department_editor_result(dialog.result))
            self._refresh_table()

    def edit_item(self):
        indexes = self._selected_department_indexes()
        if not indexes:
            return

        row = indexes[0]

        dialog = DepartmentEditorDialog(
            self,
            location_names=self.location_names,
            waste_stream_names=self.waste_stream_names,
            current_floor=self.current_floor,
            seed=self.items[row],
            default_department_id=str(self.items[row].get("id", "")),
            group_resolver=self.group_resolver,
            default_x=float(self.items[row].get("x", 0.0)),
            default_y=float(self.items[row].get("y", 0.0)),
            task_generation_categories=self.task_generation_categories,
        )

        if dialog.exec() == QDialog.Accepted and dialog.result:
            new_id = str(dialog.result.get("id", "")).strip()
            new_name = str(dialog.result.get("name", "")).strip()

            for idx, item in enumerate(self.items):
                if idx == row:
                    continue
                if str(item.get("id", "")).strip() == new_id:
                    QMessageBox.critical(
                        self, "Duplicate", "Department ID already exists"
                    )
                    return
                if str(item.get("name", "")).strip() == new_name:
                    QMessageBox.critical(
                        self, "Duplicate", "Department name already exists"
                    )
                    return

            self.items[row] = self._commit_department_editor_result(dialog.result)
            self._refresh_table()

    def delete_item(self):
        indexes = self._selected_department_indexes()
        if not indexes:
            return

        if (
            QMessageBox.question(
                self,
                "Delete departments",
                f"Delete {len(indexes)} selected department(s)?",
            )
            != QMessageBox.Yes
        ):
            return

        for idx in reversed(indexes):
            if 0 <= idx < len(self.items):
                del self.items[idx]

        self._refresh_table()

    def save_items(self):
        self.items = self._clean_department_items_for_save()
        self.on_save(self.items)
        self.accept()


class ZoomableInventoryView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._middle_panning = False
        self._last_middle_pos = None
        self._inventory_mouse_press = None
        self._inventory_mouse_move = None
        self._inventory_mouse_release = None
        self.setDragMode(QGraphicsView.NoDrag)

    def set_inventory_mouse_handlers(self, press, move, release):
        self._inventory_mouse_press = press
        self._inventory_mouse_move = move
        self._inventory_mouse_release = release

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._middle_panning = True
            self._last_middle_pos = event.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        if self._inventory_mouse_press:
            self._inventory_mouse_press(event)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._middle_panning and self._last_middle_pos is not None:
            current = event.position().toPoint()
            delta = current - self._last_middle_pos

            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )

            self._last_middle_pos = current
            event.accept()
            return

        if self._inventory_mouse_move:
            self._inventory_mouse_move(event)
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._middle_panning = False
            self._last_middle_pos = None
            self.viewport().unsetCursor()
            event.accept()
            return

        if self._inventory_mouse_release:
            self._inventory_mouse_release(event)
            return

        super().mouseReleaseEvent(event)



class ArrayInventorySpacesDialog(QDialog):
    def __init__(self, parent, payload_names, amr_names, default_kind="payload", default_name=""):
        super().__init__(parent)
        self.setWindowTitle("Create inventory space array")
        self.resize(520, 320)
        self.result = None
        self.payload_names = list(payload_names or [])
        self.amr_names = list(amr_names or [])

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["Payload", "AMR"])
        self.name_combo = QComboBox()

        self.count_edit = _integer_input(
            4, minimum=1, maximum=100_000, suffix=" space(s)",
            tooltip="Total number of inventory spaces to create.",
        )
        self.columns_edit = _integer_input(
            4, minimum=1, maximum=10_000, suffix=" column(s)",
            tooltip="Number of columns used when Grid alignment is selected.",
        )
        self.spacing_x_edit = _double_input(
            0.1, minimum=0.0, maximum=1000.0, decimals=3,
            suffix=" m", step=0.05,
            tooltip="Clear horizontal gap between adjacent spaces.",
        )
        self.spacing_y_edit = _double_input(
            0.1, minimum=0.0, maximum=1000.0, decimals=3,
            suffix=" m", step=0.05,
            tooltip="Clear vertical gap between adjacent spaces.",
        )
        self.rotation_edit = _double_input(
            0.0, minimum=-360.0, maximum=360.0, decimals=1,
            suffix="°", step=5.0,
            tooltip="Rotation applied to every space in the array.",
        )

        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["Horizontal", "Vertical", "Grid"])

        form.addRow("Type", self.kind_combo)
        form.addRow("Payload / AMR", self.name_combo)
        form.addRow("Quantity", self.count_edit)
        form.addRow("Alignment", self.alignment_combo)
        form.addRow("Grid columns", self.columns_edit)
        form.addRow("Horizontal spacing (m)", self.spacing_x_edit)
        form.addRow("Vertical spacing (m)", self.spacing_y_edit)
        form.addRow("Rotation °", self.rotation_edit)

        hint = QLabel(
            "Spacing is the clear gap between adjacent AMR/payload footprints. "
            "Grid uses the column count, then wraps to the next row."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.kind_combo.currentTextChanged.connect(self._refresh_names)
        self.alignment_combo.currentTextChanged.connect(self._update_alignment_fields)
        self._update_alignment_fields()
        if str(default_kind).strip().lower() == "amr":
            self.kind_combo.setCurrentText("AMR")
        else:
            self.kind_combo.setCurrentText("Payload")
        self._refresh_names()
        if default_name:
            self.name_combo.setCurrentText(str(default_name))
        _polish_dialog(self)

    def _refresh_names(self):
        current = self.name_combo.currentText().strip() if hasattr(self, "name_combo") else ""
        self.name_combo.clear()
        names = self.amr_names if self.kind_combo.currentText() == "AMR" else self.payload_names
        self.name_combo.addItems(names)
        if current:
            self.name_combo.setCurrentText(current)

    def _update_alignment_fields(self):
        is_grid = self.alignment_combo.currentText().strip().lower() == "grid"
        self.columns_edit.setEnabled(is_grid)
        self.columns_edit.setToolTip(
            "Only used for Grid alignment." if is_grid else
            "Ignored for Horizontal and Vertical alignment."
        )

    def accept(self):
        try:
            kind = "amr" if self.kind_combo.currentText() == "AMR" else "payload"
            name = self.name_combo.currentText().strip()
            if not name:
                raise ValueError("Select a payload or AMR type.")
            count = int(self.count_edit.value())
            alignment = self.alignment_combo.currentText().strip().lower()
            columns = int(self.columns_edit.value()) if alignment == "grid" else count
            spacing_x = float(self.spacing_x_edit.value())
            spacing_y = float(self.spacing_y_edit.value())
            rotation = float(self.rotation_edit.value())
            self.result = {
                "kind": kind,
                "name": name,
                "count": count,
                "columns": columns,
                "spacing_x": spacing_x,
                "spacing_y": spacing_y,
                "rotation_deg": rotation,
                "alignment": alignment,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid array settings", str(exc))


class InventorySpacesDialog(QDialog):
    def __init__(self, parent, location_name):
        super().__init__(parent)
        self.setWindowTitle(f"Inventory Spaces - {location_name}")
        self.resize(900, 650)

        self.location_name = location_name
        self.store = parent.store
        self.editor = parent
        self._dxf_background_pixmap = None
        self._dxf_background_rect = None
        self.location = self.store.get_location(location_name)
        self.spaces = self.store.get_location_inventory_spaces(location_name)

        self.current_points = []
        self.selected_space_index = None
        self.selected_space_indices = set()
        self.rotate_space_index = None
        self.rotate_start_center = None
        self.drag_point_index = None
        self.drag_whole_space = False
        self.drag_start_world = None
        self.drag_start_points = []
        self.drag_payload_index = None
        self.drag_payload_start_world = None
        self.drag_payload_start = None
        self.drag_space_indices = set()
        self.drag_spaces_start_world = None
        self.drag_spaces_start_slots = {}
        self.drag_space_indices = set()
        self.drag_spaces_start_world = None
        self.drag_spaces_start_slots = {}
        self.rotate_space_index = None
        self.rotate_start_center = None
        self.copied_space = None
        self._initial_fit_done = False
        self.selected_payload_index = None
        self.drag_payload_index = None
        self.drag_payload_start_world = None
        self.drag_payload_start = None
        self.drag_space_indices = set()
        self.drag_spaces_start_world = None
        self.drag_spaces_start_slots = {}

        root_layout = QVBoxLayout(self)
        layout = QHBoxLayout()
        root_layout.addLayout(layout, 1)

        left = QVBoxLayout()
        layout.addLayout(left, 0)

        self.space_list = QListWidget()
        self.space_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.space_list.currentRowChanged.connect(self.select_space)
        self.space_list.itemSelectionChanged.connect(self._sync_space_list_selection)
        self.space_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.space_list.customContextMenuRequested.connect(self._show_space_list_menu)

        left.addWidget(QLabel("Inventory spaces"))
        left.addWidget(self.space_list, 1)

        add_btn = QPushButton("New space")
        add_btn.setVisible(False)
        save_btn = QPushButton("Save current")
        copy_btn = QPushButton("Copy selected")
        paste_btn = QPushButton("Paste copy")
        delete_btn = QPushButton("Delete selected")
        finish_btn = QPushButton("Save and close")

        add_btn.clicked.connect(self.new_space)
        save_btn.clicked.connect(self.save_current_space)
        copy_btn.clicked.connect(self.copy_selected_space)
        paste_btn.clicked.connect(self.paste_copied_space)
        delete_btn.clicked.connect(self.delete_selected_space)
        finish_btn.clicked.connect(self.finish)

        left.addWidget(add_btn)
        left.addWidget(save_btn)
        left.addWidget(copy_btn)
        left.addWidget(paste_btn)
        left.addWidget(delete_btn)
        left.addStretch(1)
        left.addWidget(finish_btn)

        right = QVBoxLayout()
        layout.addLayout(right, 1)

        self.name_edit = QLineEdit()
        right.addWidget(QLabel("Space name"))
        right.addWidget(self.name_edit)

        self.rectangle_snap_check = QCheckBox("Rectangular snap")
        self.rectangle_snap_check.setChecked(True)
        self.rectangle_snap_check.setVisible(False)
        right.addWidget(self.rectangle_snap_check)

        size_row = QHBoxLayout()

        self.length_edit = QLineEdit("0.000")
        self.width_edit = QLineEdit("0.000")
        self.lock_size_check = QCheckBox("Lock size")

        self.length_edit.setReadOnly(True)
        self.width_edit.setReadOnly(True)
        self.length_edit.editingFinished.connect(self._apply_size_from_fields)
        self.width_edit.editingFinished.connect(self._apply_size_from_fields)

        size_row.addWidget(QLabel("Length"))
        size_row.addWidget(self.length_edit)
        size_row.addWidget(QLabel("Width"))
        size_row.addWidget(self.width_edit)
        size_row.addWidget(self.lock_size_check)

        right.addLayout(size_row)

        rotate_row = QHBoxLayout()
        self.rotation_edit = QLineEdit("0.0")
        self.rotation_edit.setToolTip(
            "Free-angle rotation in degrees for the selected payload inventory space"
        )
        self.rotation_edit.editingFinished.connect(self.apply_rotation_from_field)
        rotate_left_btn = QPushButton("-5°")
        rotate_right_btn = QPushButton("+5°")
        rotate_left_btn.clicked.connect(lambda: self.nudge_selected_rotation(-5.0))
        rotate_right_btn.clicked.connect(lambda: self.nudge_selected_rotation(5.0))
        rotate_row.addWidget(QLabel("Rotation °"))
        rotate_row.addWidget(self.rotation_edit)
        rotate_row.addWidget(rotate_left_btn)
        rotate_row.addWidget(rotate_right_btn)
        right.addLayout(rotate_row)

        payload_tools = QHBoxLayout()
        self.payload_combo = QComboBox()
        self.payload_combo.addItems([""] + self._payload_names())
        add_payload_btn = QPushButton("Add payload")
        auto_align_btn = QPushButton("Auto align")
        add_payload_btn.clicked.connect(self.add_payload_slot)
        auto_align_btn.clicked.connect(self.auto_align_payloads)
        payload_tools.addWidget(QLabel("Payload"))
        payload_tools.addWidget(self.payload_combo, 1)
        payload_tools.addWidget(add_payload_btn)
        payload_tools.addWidget(auto_align_btn)
        right.addLayout(payload_tools)

        amr_tools = QHBoxLayout()
        self.amr_space_combo = QComboBox()
        self.amr_space_combo.addItems([""] + self._amr_type_names())
        add_amr_space_btn = QPushButton("Add AMR space")
        add_amr_space_btn.setToolTip("Create an inventory space sized to the selected AMR so this location can store / park AMRs.")
        add_amr_space_btn.clicked.connect(self.add_amr_space)
        array_spaces_btn = QPushButton("Array...")
        array_spaces_btn.setToolTip("Create multiple payload or AMR spaces with a chosen spacing and alignment.")
        array_spaces_btn.clicked.connect(self.create_space_array)
        amr_tools.addWidget(QLabel("AMR"))
        amr_tools.addWidget(self.amr_space_combo, 1)
        amr_tools.addWidget(add_amr_space_btn)
        amr_tools.addWidget(array_spaces_btn)
        right.addLayout(amr_tools)

        capacity_tools = QHBoxLayout()
        self.charger_check = QCheckBox("Selected AMR space contains a charger")
        self.charger_check.setToolTip(
            "Only AMR spaces with this enabled can recharge an AMR. Other AMR spaces remain parking/stowage bays."
        )
        auto_arrange_btn = QPushButton("Auto arrange payload capacity...")
        auto_arrange_btn.setToolTip(
            "Create the requested number of payload spaces while reserving AMR approach and movement clearance."
        )
        auto_arrange_btn.clicked.connect(self.auto_arrange_payload_capacity)
        capacity_tools.addWidget(self.charger_check)
        capacity_tools.addStretch(1)
        capacity_tools.addWidget(auto_arrange_btn)
        right.addLayout(capacity_tools)

        self.scene = QGraphicsScene(self)
        self.view = ZoomableInventoryView(self.scene)
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setRenderHint(self.view.renderHints())
        self.view.setMouseTracking(True)
        self.view.set_inventory_mouse_handlers(
            self._mouse_press,
            self._mouse_move,
            self._mouse_release,
        )
        right.addWidget(self.view, 1)

        self.status_label = QLabel(
            "Inventory spaces are fixed footprints. Use Add payload or Add AMR space, then drag to position it or use the rotate handle / angle field."
        )
        right.addWidget(self.status_label)

        self.refresh_list()
        self.refresh_scene()
        _polish_dialog(self)

    def _sync_space_list_selection(self):
        rows = set()
        for item in self.space_list.selectedItems():
            row = self.space_list.row(item)
            if row >= 0:
                rows.add(row)
        if rows:
            self.selected_space_indices = rows
        elif self.selected_space_index is not None:
            self.selected_space_indices = {self.selected_space_index}
        self.refresh_scene()

    def _set_space_selection(self, indices, current_index=None):
        clean = {int(i) for i in indices if 0 <= int(i) < len(self.spaces)}
        if current_index is not None and 0 <= int(current_index) < len(self.spaces):
            clean.add(int(current_index))
            self.selected_space_index = int(current_index)
        self.selected_space_indices = clean

        self.space_list.blockSignals(True)
        self.space_list.clearSelection()
        for index in sorted(clean):
            item = self.space_list.item(index)
            if item is not None:
                item.setSelected(True)
        if (
            self.selected_space_index is not None
            and 0 <= self.selected_space_index < self.space_list.count()
        ):
            self.space_list.setCurrentRow(self.selected_space_index)
        self.space_list.blockSignals(False)

    def _space_payload_slot(self, index):
        if not (0 <= int(index) < len(self.spaces)):
            return None
        slots = self.spaces[int(index)].setdefault("payload_slots", [])
        return slots[0] if slots else None

    def _space_slot_polygon_points(self, index):
        slot = self._space_payload_slot(index)
        if not slot:
            return []
        return self._slot_polygon_points(slot)

    def _nearest_space_index_at(self, x, y):
        for index in reversed(range(len(self.spaces))):
            pts = self._space_slot_polygon_points(index)
            if pts and self._point_in_polygon(x, y, pts):
                return index
            pts_abs = self.store.inventory_space_points_absolute(
                self.location_name, self.spaces[index]
            )
            if pts_abs and self._point_in_polygon(x, y, pts_abs):
                return index
        return None

    def _space_group_fits_location_box(self, starts, dx, dy):
        location_box = self.store.get_location_bounding_box_points(self.location_name)
        if len(location_box) < 3:
            return True
        for index, slot_start in starts.items():
            candidate = dict(slot_start)
            sx, sy = self._slot_center_absolute(slot_start)
            self._set_slot_center_absolute(candidate, sx + dx, sy + dy)
            for p in self._slot_polygon_points(candidate):
                if not self._point_in_polygon(p["x"], p["y"], location_box):
                    return False
        return True

    def _move_space_group(self, starts, dx, dy):
        # Deliberately allow movement even when the footprint is outside the
        # location boundary.  Earlier versions rejected any candidate whose
        # corners were not inside the boundary, which meant an accidentally
        # placed payload/AMR space could not be dragged back into the valid area.
        # Boundary checking is now advisory/visual only; the user can recover an
        # out-of-bound space by dragging it back inside.
        for index, slot_start in starts.items():
            slot = self._space_payload_slot(index)
            if not slot:
                continue
            sx, sy = self._slot_center_absolute(slot_start)
            candidate = dict(slot_start)
            self._set_slot_center_absolute(candidate, sx + dx, sy + dy)
            slot.update(candidate)
            self._sync_space_from_payload_index(index)
        return True

    def _payload_names(self):
        return sorted(
            str(p.get("name", "")).strip()
            for p in self.store.data.get("payloads", [])
            if str(p.get("name", "")).strip()
        )

    def _payload_by_name(self, name):
        name = str(name or "").strip()
        for payload in self.store.data.get("payloads", []):
            if str(payload.get("name", "")).strip() == name:
                return payload
        return None

    def _payload_dimensions(self, name):
        payload = self._payload_by_name(name)
        if not payload:
            return 0.0, 0.0
        length = float(payload.get("length_m", 0.0) or 0.0)
        width = float(payload.get("width_m", 0.0) or 0.0)
        return max(0.0, length), max(0.0, width)

    def _amr_type_names(self):
        return sorted(
            str(amr.get("id", "")).strip()
            for amr in self.store.data.get("amrs", [])
            if str(amr.get("id", "")).strip()
        )

    def _amr_type_by_name(self, name):
        name = str(name or "").strip()
        for amr in self.store.data.get("amrs", []):
            if str(amr.get("id", "")).strip() == name:
                return amr
        return None

    def _amr_dimensions(self, name):
        amr = self._amr_type_by_name(name)
        if not amr:
            return 0.0, 0.0
        length = float(amr.get("length_m", 0.0) or 0.0)
        width = float(amr.get("width_m", 0.0) or 0.0)
        return max(0.0, length), max(0.0, width)

    def _slot_label(self, slot):
        if str(slot.get("slot_type", "") or "").strip().lower() == "amr":
            return str(slot.get("amr_type", "") or slot.get("amr", "") or "AMR").strip()
        return str(slot.get("payload", "") or "").strip()

    def _slot_dimensions(self, slot):
        if str(slot.get("slot_type", "") or "").strip().lower() == "amr":
            return self._amr_dimensions(str(slot.get("amr_type", "") or slot.get("amr", "")))
        return self._payload_dimensions(str(slot.get("payload", "") or ""))

    def _current_space_slots(self):
        if self.selected_space_index is None:
            return []
        if 0 <= self.selected_space_index < len(self.spaces):
            return self.spaces[self.selected_space_index].setdefault(
                "payload_slots", []
            )
        return []

    def _space_bounds(self):
        if not self.current_points:
            return None
        xs = [float(p["x"]) for p in self.current_points]
        ys = [float(p["y"]) for p in self.current_points]
        return min(xs), min(ys), max(xs), max(ys)

    def _slot_center_absolute(self, slot):
        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        if "dx" in slot and "dy" in slot:
            return lx + float(slot.get("dx", 0.0)), ly + float(slot.get("dy", 0.0))
        return float(slot.get("x", lx)), float(slot.get("y", ly))

    def _set_slot_center_absolute(self, slot, x, y):
        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        slot["dx"] = round(float(x) - lx, 3)
        slot["dy"] = round(float(y) - ly, 3)
        slot.pop("x", None)
        slot.pop("y", None)

    def _payload_slot_rect_points(self, slot, padding=0.0):
        length, width = self._slot_dimensions(slot)
        if length <= 0 or width <= 0:
            return []
        cx, cy = self._slot_center_absolute(slot)
        rotation = float(slot.get("rotation_deg", 0.0) or 0.0) % 180.0
        # Inventory space footprint follows the fixed payload size.  A 90°
        # rotation swaps the axis-aligned stored footprint so the simulator and
        # reports see the same usable inventory rectangle that the user sees.
        if abs(rotation - 90.0) < 1e-6:
            length, width = width, length
        length += float(padding) * 2.0
        width += float(padding) * 2.0
        return [
            {"x": round(cx - (length / 2.0), 3), "y": round(cy - (width / 2.0), 3)},
            {"x": round(cx + (length / 2.0), 3), "y": round(cy - (width / 2.0), 3)},
            {"x": round(cx + (length / 2.0), 3), "y": round(cy + (width / 2.0), 3)},
            {"x": round(cx - (length / 2.0), 3), "y": round(cy + (width / 2.0), 3)},
        ]

    def _sync_current_space_from_payload(self):
        slots = self._current_space_slots()
        if not slots:
            return False
        points = self._slot_polygon_points(slots[0])
        if len(points) < 3:
            return False
        self.current_points = points
        return True

    def _commit_current_space(self, show_errors=False):
        if self.selected_space_index is None:
            return True
        if not (0 <= self.selected_space_index < len(self.spaces)):
            return True

        # For payload-only spaces, the stored space polygon is always derived
        # from the payload slot.
        self._sync_current_space_from_payload()

        if len(self.current_points) < 3:
            if show_errors:
                QMessageBox.critical(
                    self,
                    "Invalid inventory space",
                    "Inventory spaces must be created from a payload footprint.",
                )
            return False

        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        name = self.name_edit.text().strip() or self.spaces[
            self.selected_space_index
        ].get("name", f"Inventory {self.selected_space_index + 1}")
        existing_space = self.spaces[self.selected_space_index]
        slots_copy = [dict(slot) for slot in existing_space.get("payload_slots", [])]
        stores_amr = bool(existing_space.get("stores_amr", False)) or str(existing_space.get("space_type", "")).lower() == "amr"
        if slots_copy and str(slots_copy[0].get("slot_type", "")).lower() == "amr":
            stores_amr = True
        updated_space = {
            "name": name,
            "points": [
                {
                    "dx": round(float(p["x"]) - lx, 3),
                    "dy": round(float(p["y"]) - ly, 3),
                }
                for p in self.current_points
            ],
            "payload_slots": slots_copy,
        }
        for key in ("occupied", "payload", "payload_instance_id", "reserved_by_task", "task_id", "amr_id", "reserved_by_amr"):
            if key in existing_space:
                updated_space[key] = existing_space.get(key)
        if stores_amr:
            updated_space["space_type"] = "amr"
            updated_space["stores_amr"] = True
            updated_space["amr_type"] = str(existing_space.get("amr_type", "") or (slots_copy[0].get("amr_type", "") if slots_copy else ""))
            updated_space["has_charger"] = bool(self.charger_check.isChecked())
        self.spaces[self.selected_space_index] = updated_space
        return True

    def _slot_polygon_points(self, slot):
        length, width = self._slot_dimensions(slot)
        if length <= 0 or width <= 0:
            return []
        cx, cy = self._slot_center_absolute(slot)
        angle = math.radians(float(slot.get("rotation_deg", 0.0) or 0.0))
        c = math.cos(angle)
        s = math.sin(angle)
        corners = [
            (-length / 2.0, -width / 2.0),
            (length / 2.0, -width / 2.0),
            (length / 2.0, width / 2.0),
            (-length / 2.0, width / 2.0),
        ]
        return [
            {
                "x": round(cx + (dx * c) - (dy * s), 3),
                "y": round(cy + (dx * s) + (dy * c), 3),
            }
            for dx, dy in corners
        ]

    def _point_in_polygon(self, x, y, points):
        if len(points) < 3:
            return False
        inside = False
        j = len(points) - 1
        for i in range(len(points)):
            xi = float(points[i]["x"])
            yi = float(points[i]["y"])
            xj = float(points[j]["x"])
            yj = float(points[j]["y"])
            if ((yi > y) != (yj > y)) and (
                x < ((xj - xi) * (y - yi) / ((yj - yi) or 1e-9)) + xi
            ):
                inside = not inside
            j = i
        return inside

    def _slot_contains_point(self, slot, x, y):
        return self._point_in_polygon(x, y, self._slot_polygon_points(slot))

    def _nearest_payload_slot_index(self, x, y):
        for idx in reversed(range(len(self._current_space_slots()))):
            if self._slot_contains_point(self._current_space_slots()[idx], x, y):
                return idx
        return None

    def _slot_fits_current_space(self, slot):
        if not self.current_points:
            return True
        return all(
            self._point_in_polygon(p["x"], p["y"], self.current_points)
            for p in self._slot_polygon_points(slot)
        )

    def add_payload_slot(self):
        payload_name = self.payload_combo.currentText().strip()
        if not payload_name:
            QMessageBox.information(self, "Payload", "Select a payload first.")
            return

        length, width = self._payload_dimensions(payload_name)
        if length <= 0 or width <= 0:
            QMessageBox.critical(
                self,
                "Payload",
                "Payload length and width must be greater than zero.",
            )
            return

        # Add payload now creates a dedicated inventory space that is exactly
        # the fixed footprint of the selected payload.  The payload slot is
        # centred inside that space, so it can still be moved/rotated with the
        # existing payload-edit tools if required.
        cx, cy = self._next_payload_space_centre(length, width)
        min_x = cx - (length / 2.0)
        min_y = cy - (width / 2.0)
        max_x = cx + (length / 2.0)
        max_y = cy + (width / 2.0)

        points_abs = [
            {"x": round(min_x, 3), "y": round(min_y, 3)},
            {"x": round(max_x, 3), "y": round(min_y, 3)},
            {"x": round(max_x, 3), "y": round(max_y, 3)},
            {"x": round(min_x, 3), "y": round(max_y, 3)},
        ]

        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        name = self._next_payload_space_name(payload_name)
        space = {
            "name": name,
            "points": [
                {
                    "dx": round(float(p["x"]) - lx, 3),
                    "dy": round(float(p["y"]) - ly, 3),
                }
                for p in points_abs
            ],
            "payload_slots": [
                {
                    "payload": payload_name,
                    "dx": round(cx - lx, 3),
                    "dy": round(cy - ly, 3),
                    "rotation_deg": 0.0,
                }
            ],
        }

        self.spaces.append(space)
        self.selected_space_index = len(self.spaces) - 1
        self.name_edit.setText(name)
        self._sync_current_space_from_payload()
        self.selected_payload_index = 0
        self.selected_space_indices = {self.selected_space_index}
        self.lock_size_check.setChecked(True)
        self.refresh_list()
        self.space_list.setCurrentRow(self.selected_space_index)
        self.refresh_scene()
        self.status_label.setText(
            f"Created inventory space '{name}' sized to {payload_name} "
            f"({length:.3f} m × {width:.3f} m)."
        )

    def add_amr_space(self):
        amr_type = self.amr_space_combo.currentText().strip()
        if not amr_type:
            QMessageBox.information(self, "AMR space", "Select an AMR type first.")
            return

        length, width = self._amr_dimensions(amr_type)
        if length <= 0 or width <= 0:
            QMessageBox.critical(
                self,
                "AMR space",
                "AMR length and width must be greater than zero.",
            )
            return

        cx, cy = self._next_payload_space_centre(length, width)
        min_x = cx - (length / 2.0)
        min_y = cy - (width / 2.0)
        max_x = cx + (length / 2.0)
        max_y = cy + (width / 2.0)
        points_abs = [
            {"x": round(min_x, 3), "y": round(min_y, 3)},
            {"x": round(max_x, 3), "y": round(min_y, 3)},
            {"x": round(max_x, 3), "y": round(max_y, 3)},
            {"x": round(min_x, 3), "y": round(max_y, 3)},
        ]
        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        name = self._next_payload_space_name(f"{amr_type} AMR")
        space = {
            "name": name,
            "space_type": "amr",
            "stores_amr": True,
            "has_charger": False,
            "points": [
                {"dx": round(float(p["x"]) - lx, 3), "dy": round(float(p["y"]) - ly, 3)}
                for p in points_abs
            ],
            "payload_slots": [
                {
                    "slot_type": "amr",
                    "amr_type": amr_type,
                    "dx": round(cx - lx, 3),
                    "dy": round(cy - ly, 3),
                    "rotation_deg": 0.0,
                }
            ],
        }

        self.spaces.append(space)
        self.selected_space_index = len(self.spaces) - 1
        self.name_edit.setText(name)
        self._sync_current_space_from_payload()
        self.selected_payload_index = 0
        self.selected_space_indices = {self.selected_space_index}
        self.lock_size_check.setChecked(True)
        self.refresh_list()
        self.space_list.setCurrentRow(self.selected_space_index)
        self.refresh_scene()
        self.status_label.setText(
            f"Created AMR storage space '{name}' sized to {amr_type} "
            f"({length:.3f} m × {width:.3f} m)."
        )

    def _space_template_for_kind(self, kind, name, cx, cy, rotation_deg=0.0, name_override=""):
        kind = str(kind or "payload").strip().lower()
        if kind == "amr":
            length, width = self._amr_dimensions(name)
            slot = {
                "slot_type": "amr",
                "amr_type": name,
                "dx": 0.0,
                "dy": 0.0,
                "rotation_deg": self._normalise_degrees(rotation_deg),
            }
            base_name = f"{name} AMR"
            extra = {"space_type": "amr", "stores_amr": True, "has_charger": False}
        else:
            length, width = self._payload_dimensions(name)
            slot = {
                "payload": name,
                "dx": 0.0,
                "dy": 0.0,
                "rotation_deg": self._normalise_degrees(rotation_deg),
            }
            base_name = name
            extra = {}

        if length <= 0 or width <= 0:
            raise ValueError(f"{name} length and width must be greater than zero.")

        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        slot["dx"] = round(float(cx) - lx, 3)
        slot["dy"] = round(float(cy) - ly, 3)
        temp_slot = dict(slot)
        points_abs = self._slot_polygon_points(temp_slot)
        if len(points_abs) < 3:
            min_x = float(cx) - (length / 2.0)
            min_y = float(cy) - (width / 2.0)
            max_x = float(cx) + (length / 2.0)
            max_y = float(cy) + (width / 2.0)
            points_abs = [
                {"x": min_x, "y": min_y},
                {"x": max_x, "y": min_y},
                {"x": max_x, "y": max_y},
                {"x": min_x, "y": max_y},
            ]

        space = {
            "name": name_override or self._next_payload_space_name(base_name),
            "points": [
                {"dx": round(float(p["x"]) - lx, 3), "dy": round(float(p["y"]) - ly, 3)}
                for p in points_abs
            ],
            "payload_slots": [slot],
        }
        space.update(extra)
        return space, length, width

    def create_space_array(self):
        default_kind = "amr" if self.amr_space_combo.currentText().strip() else "payload"
        default_name = self.amr_space_combo.currentText().strip() or self.payload_combo.currentText().strip()
        dialog = ArrayInventorySpacesDialog(
            self,
            self._payload_names(),
            self._amr_type_names(),
            default_kind=default_kind,
            default_name=default_name,
        )
        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return

        cfg = dialog.result
        kind = cfg["kind"]
        item_name = cfg["name"]
        if kind == "amr":
            length, width = self._amr_dimensions(item_name)
        else:
            length, width = self._payload_dimensions(item_name)
        if length <= 0 or width <= 0:
            QMessageBox.critical(self, "Create array", f"{item_name} length and width must be greater than zero.")
            return

        count = int(cfg.get("count", 1) or 1)
        spacing_x = float(cfg.get("spacing_x", 0.0) or 0.0)
        spacing_y = float(cfg.get("spacing_y", 0.0) or 0.0)
        alignment = str(cfg.get("alignment", "horizontal") or "horizontal").lower()
        columns = int(cfg.get("columns", count) or count)
        rotation = self._normalise_degrees(cfg.get("rotation_deg", 0.0))

        start_cx, start_cy = self._next_payload_space_centre(length, width)

        # Array distribution follows the scene axes deliberately.
        # The copied AMR/payload spaces keep the requested rotation, but
        # Horizontal means positive scene X and Vertical means positive scene Y.
        # Spacing is measured between the visible axis-aligned extents of the
        # rotated footprint, so rotated items do not visually overlap while the
        # array direction still ignores rotation.
        angle = math.radians(rotation)
        c = abs(math.cos(angle))
        s = abs(math.sin(angle))
        footprint_scene_width = (float(length) * c) + (float(width) * s)
        footprint_scene_height = (float(length) * s) + (float(width) * c)
        step_x = footprint_scene_width + spacing_x
        step_y = footprint_scene_height + spacing_y

        created_indexes = []
        errors = []
        existing_count = len(self.spaces)

        for i in range(count):
            if alignment == "vertical":
                col = 0
                row = i
            elif alignment == "grid":
                col = i % max(1, columns)
                row = i // max(1, columns)
            else:
                col = i
                row = 0

            cx = start_cx + (col * step_x)
            cy = start_cy + (row * step_y)
            try:
                display_index = existing_count + i + 1
                base_label = f"{item_name} AMR" if kind == "amr" else item_name
                name = f"{base_label} space {display_index}"
                space, _length, _width = self._space_template_for_kind(
                    kind,
                    item_name,
                    cx,
                    cy,
                    rotation_deg=rotation,
                    name_override=name,
                )
                self.spaces.append(space)
                created_indexes.append(len(self.spaces) - 1)
            except Exception as exc:
                errors.append(str(exc))

        if created_indexes:
            self.selected_space_index = created_indexes[0]
            self.selected_space_indices = set(created_indexes)
            self.name_edit.setText(self.spaces[self.selected_space_index].get("name", ""))
            self.selected_payload_index = 0
            self.lock_size_check.setChecked(True)
            self.refresh_list()
            self._set_space_selection(created_indexes, current_index=self.selected_space_index)
            self.select_space(self.selected_space_index)
            self.refresh_scene()

        message = f"Created {len(created_indexes)} {item_name} space(s)."
        if errors:
            message += f" {len(errors)} item(s) could not be created."
        self.status_label.setText(message)

    def _next_payload_space_name(self, payload_name):
        base = str(payload_name).strip() or "Payload"
        existing = {str(space.get("name", "")).strip() for space in self.spaces}
        index = 1
        while True:
            name = f"{base} space {index}"
            if name not in existing:
                return name
            index += 1

    def _next_payload_space_centre(self, length, width):
        # Prefer placing inside the location bounding box when one exists;
        # otherwise start at the location origin.  Each added payload is
        # offset by one payload width/length plus a small gap so new spaces do
        # not sit exactly on top of each other.
        gap = 0.1
        location_box = self.store.get_location_bounding_box_points(self.location_name)

        if location_box:
            xs = [float(p["x"]) for p in location_box]
            ys = [float(p["y"]) for p in location_box]
            min_x = min(xs)
            max_x = max(xs)
            min_y = min(ys)
            max_y = max(ys)

            usable_width = max(0.0, max_x - min_x)
            columns = max(1, int((usable_width + gap) // (float(length) + gap)))
            index = len(self.spaces)
            col = index % columns
            row = index // columns
            cx = min_x + (float(length) / 2.0) + (col * (float(length) + gap))
            cy = min_y + (float(width) / 2.0) + (row * (float(width) + gap))

            # If the calculated row would run beyond the bounding box, keep it
            # visible by falling back to the lower-left origin plus stagger.
            if cy + (float(width) / 2.0) <= max_y + 1e-9:
                return round(cx, 3), round(cy, 3)

        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        offset = len(self.spaces) * (max(float(length), float(width)) + gap)
        return round(lx + (float(length) / 2.0) + offset, 3), round(
            ly + (float(width) / 2.0), 3
        )

    def _sync_space_from_payload_index(self, index):
        if not (0 <= int(index) < len(self.spaces)):
            return False

        space = self.spaces[int(index)]
        slots = space.setdefault("payload_slots", [])
        if not slots:
            return False

        points = self._slot_polygon_points(slots[0])
        if len(points) < 3:
            return False

        lx = float(self.location.get("x", 0.0))
        ly = float(self.location.get("y", 0.0))
        space["points"] = [
            {
                "dx": round(float(p["x"]) - lx, 3),
                "dy": round(float(p["y"]) - ly, 3),
            }
            for p in points
        ]

        if index == self.selected_space_index:
            self.current_points = points

        return True

    def auto_arrange_payload_capacity(self):
        location_box = self.store.get_location_bounding_box_points(self.location_name)
        if len(location_box) < 3:
            QMessageBox.critical(
                self,
                "Auto arrange payload capacity",
                "Draw or define the location bounding box first.",
            )
            return
        payload_name = self.payload_combo.currentText().strip()
        if not payload_name:
            payload_name, ok = QInputDialog.getItem(
                self, "Payload type", "Payload to store", self._payload_names(), 0, False
            )
            if not ok or not payload_name:
                return
        count, ok = QInputDialog.getInt(
            self,
            "Payload quantity",
            "Number of payloads to store at this location",
            value=1,
            minValue=1,
            maxValue=10000,
        )
        if not ok:
            return

        payload_length, payload_width = self._payload_dimensions(payload_name)
        if payload_length <= 0 or payload_width <= 0:
            QMessageBox.critical(self, "Auto arrange payload capacity", "Payload dimensions must be greater than zero.")
            return

        amr_lengths = [float(a.get("length_m", 0.8) or 0.8) for a in self.store.data.get("amrs", [])]
        amr_widths = [float(a.get("width_m", 0.6) or 0.6) for a in self.store.data.get("amrs", [])]
        max_amr_length = max(amr_lengths or [0.8])
        max_amr_width = max(amr_widths or [0.6])
        safety_clearance = max(0.2, float(self.store.data.get("building", {}).get("inventory_access_clearance_m", 0.3) or 0.3))
        access_aisle = max_amr_width + (2.0 * safety_clearance)
        end_turning = max_amr_length + safety_clearance
        gap = safety_clearance

        xs = [float(p["x"]) for p in location_box]
        ys = [float(p["y"]) for p in location_box]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        room_length = max_x - min_x
        room_width = max_y - min_y

        # Compare two shelf orientations. One side is deliberately reserved as
        # a clear travel aisle, with an end turning/approach zone.
        candidates = []
        for item_length, item_width, rotation in ((payload_length, payload_width, 0.0), (payload_width, payload_length, 90.0)):
            usable_length = max(0.0, room_length - end_turning)
            usable_width = max(0.0, room_width - access_aisle)
            cols = int((usable_length + gap) // (item_length + gap)) if item_length > 0 else 0
            rows = int((usable_width + gap) // (item_width + gap)) if item_width > 0 else 0
            candidates.append((cols * rows, cols, rows, item_length, item_width, rotation))
        capacity, cols, rows, item_length, item_width, rotation = max(candidates, key=lambda item: item[0])

        if capacity < count:
            # Suggest a compact rectangular room using approximately square rows.
            suggest_cols = max(1, int(math.ceil(math.sqrt(count))))
            suggest_rows = int(math.ceil(count / suggest_cols))
            required_length = end_turning + (suggest_cols * item_length) + (max(0, suggest_cols - 1) * gap)
            required_width = access_aisle + (suggest_rows * item_width) + (max(0, suggest_rows - 1) * gap)
            required_area = required_length * required_width
            QMessageBox.critical(
                self,
                "Location too small",
                (
                    f"{self.location_name} can safely store approximately {capacity} {payload_name} payload(s), "
                    f"not the requested {count}.\n\n"
                    f"Current bounding size: {room_length:.2f} m × {room_width:.2f} m.\n"
                    f"Suggested minimum clear area: {required_length:.2f} m × {required_width:.2f} m "
                    f"({required_area:.2f} m²).\n\n"
                    f"This includes a {access_aisle:.2f} m AMR access aisle and a {end_turning:.2f} m approach/turning zone."
                ),
            )
            return

        # Remove existing payload spaces of this type only, leaving AMR bays and
        # other payload storage untouched.
        self._commit_current_space(show_errors=False)
        kept = []
        for space in self.spaces:
            slots = space.get("payload_slots", []) or []
            if slots and str(slots[0].get("payload", "")).strip() == payload_name:
                continue
            kept.append(space)
        self.spaces = kept

        start_x = min_x + end_turning + (item_length / 2.0)
        start_y = min_y + access_aisle + (item_width / 2.0)
        created = []
        for index in range(count):
            col = index % cols
            row = index // cols
            cx = start_x + col * (item_length + gap)
            cy = start_y + row * (item_width + gap)
            space, _length, _width = self._space_template_for_kind(
                "payload", payload_name, cx, cy, rotation_deg=rotation
            )
            self.spaces.append(space)
            created.append(len(self.spaces) - 1)

        self.selected_space_index = created[0] if created else None
        self.selected_space_indices = set(created)
        self.refresh_list()
        if self.selected_space_index is not None:
            self.space_list.setCurrentRow(self.selected_space_index)
        self.refresh_scene()
        self.status_label.setText(
            f"Auto arranged {count} {payload_name} space(s), retaining a {access_aisle:.2f} m access aisle."
        )

    def auto_align_payloads(self):
        location_box = self.store.get_location_bounding_box_points(self.location_name)
        if len(location_box) < 3:
            QMessageBox.information(
                self,
                "Auto align",
                "Draw or define the location bounding box first. Auto align arranges payload spaces within that bounding box.",
            )
            return

        self._commit_current_space(show_errors=False)

        xs = [float(p["x"]) for p in location_box]
        ys = [float(p["y"]) for p in location_box]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        gap = 0.05

        payload_spaces = []
        skipped = 0

        for index, space in enumerate(self.spaces):
            slots = space.setdefault("payload_slots", [])
            if not slots:
                skipped += 1
                continue

            slot = slots[0]
            item_name = self._slot_label(slot)
            length, width = self._slot_dimensions(slot)
            if length <= 0 or width <= 0:
                skipped += 1
                continue

            payload_spaces.append((index, slot, item_name, length, width))

        if not payload_spaces:
            QMessageBox.information(
                self,
                "Auto align",
                "There are no payload-based inventory spaces to align.",
            )
            return

        # Shelf-pack payload footprints into the location bounding box.  For each
        # payload, try the orientation that fits the current row; otherwise start
        # a new row and retry.  Oversized/overflowing payloads are left unchanged.
        x = min_x
        y = min_y
        row_height = 0.0
        placed = 0
        overflow = 0

        for index, slot, _payload_name, length, width in payload_spaces:
            orientations = [(length, width, 0.0)]
            if abs(length - width) > 1e-9:
                orientations.append((width, length, 90.0))

            chosen = None

            for item_len, item_wid, rotation in orientations:
                if (x + item_len <= max_x + 1e-9) and (y + item_wid <= max_y + 1e-9):
                    chosen = (item_len, item_wid, rotation)
                    break

            if chosen is None:
                x = min_x
                y = y + row_height + gap
                row_height = 0.0

                for item_len, item_wid, rotation in orientations:
                    if (x + item_len <= max_x + 1e-9) and (
                        y + item_wid <= max_y + 1e-9
                    ):
                        chosen = (item_len, item_wid, rotation)
                        break

            if chosen is None:
                overflow += 1
                continue

            item_len, item_wid, rotation = chosen
            cx = x + (item_len / 2.0)
            cy = y + (item_wid / 2.0)
            slot["rotation_deg"] = rotation
            self._set_slot_center_absolute(slot, cx, cy)
            self._sync_space_from_payload_index(index)

            x = x + item_len + gap
            row_height = max(row_height, item_wid)
            placed += 1

        if self.selected_space_index is not None:
            self.select_space(self.selected_space_index)

        self.refresh_list()
        self.refresh_scene()

        message = (
            f"Auto aligned {placed} payload space(s) within the location bounding box."
        )
        if overflow:
            message += (
                f" {overflow} payload space(s) did not fit and were left unchanged."
            )
        if skipped:
            message += f" {skipped} non-payload/invalid space(s) skipped."
        self.status_label.setText(message)

    def _apply_size_from_fields(self):
        if len(self.current_points) != 4:
            return

        try:
            new_length = float(self.length_edit.text())
            new_width = float(self.width_edit.text())
        except ValueError:
            self._refresh_size_fields()
            return

        if new_length <= 0 or new_width <= 0:
            self._refresh_size_fields()
            return

        xs = [float(p["x"]) for p in self.current_points]
        ys = [float(p["y"]) for p in self.current_points]

        min_x = min(xs)
        min_y = min(ys)

        self.current_points = [
            {"x": round(min_x, 3), "y": round(min_y, 3)},
            {"x": round(min_x + new_length, 3), "y": round(min_y, 3)},
            {"x": round(min_x + new_length, 3), "y": round(min_y + new_width, 3)},
            {"x": round(min_x, 3), "y": round(min_y + new_width, 3)},
        ]

        self.refresh_scene()

    def _show_space_list_menu(self, pos):
        row = self.space_list.row(self.space_list.itemAt(pos))
        if row >= 0:
            self.space_list.setCurrentRow(row)

        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        paste_action = menu.addAction("Paste")
        delete_action = menu.addAction("Delete")

        copy_action.setEnabled(self.space_list.currentRow() >= 0)
        paste_action.setEnabled(self.copied_space is not None)
        delete_action.setEnabled(self.space_list.currentRow() >= 0)

        action = menu.exec(self.space_list.viewport().mapToGlobal(pos))

        if action == copy_action:
            self.copy_selected_space()
        elif action == paste_action:
            self.paste_copied_space()
        elif action == delete_action:
            self.delete_selected_space()

    def _current_size(self):
        if not self.current_points:
            return 0.0, 0.0

        xs = [float(p["x"]) for p in self.current_points]
        ys = [float(p["y"]) for p in self.current_points]

        length = max(xs) - min(xs)
        width = max(ys) - min(ys)

        return round(length, 3), round(width, 3)

    def _refresh_size_fields(self):
        length, width = self._current_size()
        self.length_edit.setText(f"{length:.3f}")
        self.width_edit.setText(f"{width:.3f}")

    def _point_inside_current_space(self, x, y):
        if len(self.current_points) < 3:
            return False

        inside = False
        points = self.current_points
        j = len(points) - 1

        for i in range(len(points)):
            xi = float(points[i]["x"])
            yi = float(points[i]["y"])
            xj = float(points[j]["x"])
            yj = float(points[j]["y"])

            intersects = ((yi > y) != (yj > y)) and (
                x < ((xj - xi) * (y - yi) / ((yj - yi) or 1e-9)) + xi
            )

            if intersects:
                inside = not inside

            j = i

        return inside

    def refresh_list(self):
        self.space_list.blockSignals(True)
        self.space_list.clear()
        for space in self.spaces:
            self.space_list.addItem(space.get("name", "Inventory space"))
        self.space_list.blockSignals(False)

    def select_space(self, row):
        previous_index = self.selected_space_index
        if previous_index is not None and previous_index != row:
            self._commit_current_space(show_errors=False)

        if row < 0 or row >= len(self.spaces):
            return

        self.selected_space_index = row
        if not self.selected_space_indices or row not in self.selected_space_indices:
            self.selected_space_indices = {row}
        space = self.spaces[row]
        self.name_edit.setText(space.get("name", ""))

        slots = space.setdefault("payload_slots", [])
        is_amr_space = bool(space.get("stores_amr", False)) or str(space.get("space_type", "")).lower() == "amr"
        if slots and str(slots[0].get("slot_type", "")).lower() == "amr":
            is_amr_space = True
        self.charger_check.blockSignals(True)
        self.charger_check.setChecked(bool(space.get("has_charger", False)))
        self.charger_check.setEnabled(is_amr_space)
        self.charger_check.blockSignals(False)
        if slots:
            self.current_points = self._slot_polygon_points(slots[0])
            self.selected_payload_index = 0
            if str(slots[0].get("slot_type", "") or "").strip().lower() == "amr":
                if hasattr(self, "amr_space_combo"):
                    self.amr_space_combo.setCurrentText(str(slots[0].get("amr_type", "")))
            else:
                self.payload_combo.setCurrentText(str(slots[0].get("payload", "")))
            self._refresh_rotation_field()
        else:
            self.current_points = self.store.inventory_space_points_absolute(
                self.location_name,
                space,
            )
            self.selected_payload_index = None
            self._refresh_rotation_field()

        self.refresh_scene()

    def new_space(self):
        QMessageBox.information(
            self,
            "Inventory space",
            "Inventory spaces are now created from payload footprints. Select a payload and use Add payload.",
        )

    def save_current_space(self):
        if self._commit_current_space(show_errors=True):
            self.refresh_list()
            if self.selected_space_index is not None:
                self.space_list.blockSignals(True)
                self.space_list.setCurrentRow(self.selected_space_index)
                self.space_list.blockSignals(False)
            self.refresh_scene()

    def delete_selected_space(self):
        rows = sorted(
            self.selected_space_indices or {self.space_list.currentRow()}, reverse=True
        )
        rows = [row for row in rows if 0 <= row < len(self.spaces)]
        if not rows:
            return

        for row in rows:
            del self.spaces[row]
        self.selected_space_index = None
        self.selected_space_indices = set()
        self.current_points = []
        self.name_edit.clear()
        self.refresh_list()
        self.refresh_scene()

    def finish(self):
        self._commit_current_space(show_errors=False)
        self.store.set_location_inventory_spaces(self.location_name, self.spaces)
        self.accept()

    def world_to_scene(self, x, y):
        return QPointF(float(x), -float(y))

    def scene_to_world(self, point):
        return float(point.x()), -float(point.y())

    def _apply_rectangle_snap(self, moving_index=None):
        if not self.rectangle_snap_check.isChecked():
            return

        if len(self.current_points) != 4:
            return

        if moving_index is None or moving_index < 0 or moving_index >= 4:
            xs = [float(p["x"]) for p in self.current_points]
            ys = [float(p["y"]) for p in self.current_points]

            min_x = round(min(xs), 3)
            max_x = round(max(xs), 3)
            min_y = round(min(ys), 3)
            max_y = round(max(ys), 3)

            self.current_points = [
                {"x": min_x, "y": min_y},
                {"x": max_x, "y": min_y},
                {"x": max_x, "y": max_y},
                {"x": min_x, "y": max_y},
            ]
            return

        p = self.current_points[moving_index]
        opposite_index = (moving_index + 2) % 4
        opposite = self.current_points[opposite_index]

        x1 = round(float(p["x"]), 3)
        y1 = round(float(p["y"]), 3)
        x2 = round(float(opposite["x"]), 3)
        y2 = round(float(opposite["y"]), 3)

        self.current_points = [
            {"x": x1, "y": y1},
            {"x": x2, "y": y1},
            {"x": x2, "y": y2},
            {"x": x1, "y": y2},
        ]

        if moving_index == 1:
            self.current_points = [
                {"x": x2, "y": y1},
                {"x": x1, "y": y1},
                {"x": x1, "y": y2},
                {"x": x2, "y": y2},
            ]
        elif moving_index == 2:
            self.current_points = [
                {"x": x2, "y": y2},
                {"x": x1, "y": y2},
                {"x": x1, "y": y1},
                {"x": x2, "y": y1},
            ]
        elif moving_index == 3:
            self.current_points = [
                {"x": x1, "y": y2},
                {"x": x2, "y": y2},
                {"x": x2, "y": y1},
                {"x": x1, "y": y1},
            ]

    def _normalise_degrees(self, angle):
        try:
            angle = float(angle)
        except Exception:
            angle = 0.0
        angle = angle % 360.0
        if angle < 0:
            angle += 360.0
        return round(angle, 3)

    def _selected_space_slot(self):
        if self.selected_space_index is None:
            return None
        return self._space_payload_slot(self.selected_space_index)

    def _refresh_rotation_field(self):
        if not hasattr(self, "rotation_edit"):
            return
        slot = self._selected_space_slot()
        if not slot:
            self.rotation_edit.setText("0.0")
            return
        self.rotation_edit.setText(
            f"{self._normalise_degrees(slot.get('rotation_deg', 0.0)):.1f}"
        )

    def apply_rotation_from_field(self):
        slot = self._selected_space_slot()
        if not slot or self.selected_space_index is None:
            self._refresh_rotation_field()
            return
        try:
            angle = float(self.rotation_edit.text())
        except ValueError:
            self._refresh_rotation_field()
            return
        slot["rotation_deg"] = self._normalise_degrees(angle)
        self.selected_payload_index = 0
        self._sync_space_from_payload_index(self.selected_space_index)
        self._commit_current_space(show_errors=False)
        self.refresh_scene()

    def nudge_selected_rotation(self, delta):
        slot = self._selected_space_slot()
        if not slot or self.selected_space_index is None:
            return
        slot["rotation_deg"] = self._normalise_degrees(
            float(slot.get("rotation_deg", 0.0) or 0.0) + float(delta)
        )
        self.selected_payload_index = 0
        self._sync_space_from_payload_index(self.selected_space_index)
        self._commit_current_space(show_errors=False)
        self.refresh_scene()

    def _rotation_handle_world(self, slot):
        length, width = self._slot_dimensions(slot)
        if length <= 0 or width <= 0:
            return None
        cx, cy = self._slot_center_absolute(slot)
        angle = math.radians(float(slot.get("rotation_deg", 0.0) or 0.0))
        distance = (max(length, width) / 2.0) + 0.35
        return {
            "x": cx + (math.cos(angle) * distance),
            "y": cy + (math.sin(angle) * distance),
        }

    def _nearest_rotation_handle_index_at(self, x, y, radius=0.25):
        # Only the current space exposes the handle to avoid accidental rotation
        # of a space hidden under another selected payload.
        if self.selected_space_index is None:
            return None
        slot = self._selected_space_slot()
        if not slot:
            return None
        handle = self._rotation_handle_world(slot)
        if not handle:
            return None
        dist = math.hypot(float(handle["x"]) - float(x), float(handle["y"]) - float(y))
        return self.selected_space_index if dist <= radius else None

    def _nearest_current_point(self, x, y, radius=0.5):
        best = None
        best_dist = radius

        for idx, p in enumerate(self.current_points):
            d = ((float(p["x"]) - x) ** 2 + (float(p["y"]) - y) ** 2) ** 0.5
            if d <= best_dist:
                best = idx
                best_dist = d

        return best

    def _mouse_press(self, event):
        scene_pos = self.view.mapToScene(event.position().toPoint())
        x, y = self.scene_to_world(scene_pos)

        hit_space = self._nearest_space_index_at(x, y)
        modifiers = event.modifiers()
        additive = bool(modifiers & (Qt.ControlModifier | Qt.ShiftModifier))

        rotate_hit = self._nearest_rotation_handle_index_at(x, y)
        if event.button() == Qt.LeftButton and rotate_hit is not None:
            slot = self._space_payload_slot(rotate_hit)
            if slot:
                self.rotate_space_index = rotate_hit
                self.rotate_start_center = self._slot_center_absolute(slot)
                self.drag_space_indices = set()
                self.drag_spaces_start_world = None
                self.drag_spaces_start_slots = {}
                self.selected_payload_index = 0
                self.status_label.setText(
                    "Drag to rotate freely. Release to save the angle."
                )
                return

        if event.button() == Qt.RightButton:
            if hit_space is not None:
                previous_index = self.selected_space_index
                if previous_index is not None and previous_index != hit_space:
                    self._commit_current_space(show_errors=False)
                self.select_space(hit_space)
                self.status_label.setText(
                    "Use the rotation angle field, +/- buttons, or drag the rotate handle for free-angle rotation."
                )
            return

        if event.button() != Qt.LeftButton:
            return

        if hit_space is not None:
            previous_index = self.selected_space_index
            if previous_index is not None and previous_index != hit_space:
                self._commit_current_space(show_errors=False)

            if additive:
                selected = set(self.selected_space_indices)
                if hit_space in selected and len(selected) > 1:
                    selected.remove(hit_space)
                else:
                    selected.add(hit_space)
                self._set_space_selection(selected, current_index=hit_space)
            elif hit_space not in self.selected_space_indices:
                self._set_space_selection({hit_space}, current_index=hit_space)
            else:
                self.selected_space_index = hit_space

            self.select_space(hit_space)
            self.selected_payload_index = 0
            drag_indices = set(self.selected_space_indices or {hit_space})
            self.drag_space_indices = drag_indices
            self.drag_spaces_start_world = {"x": x, "y": y}
            self.drag_spaces_start_slots = {}
            for index in drag_indices:
                slot = self._space_payload_slot(index)
                if slot:
                    self.drag_spaces_start_slots[index] = dict(slot)
            self.refresh_scene()
            return

    def _mouse_move(self, event):
        scene_pos = self.view.mapToScene(event.position().toPoint())
        x, y = self.scene_to_world(scene_pos)

        # Qt can still deliver a move event after the mouse button has been
        # released, especially if the cursor leaves the rotate handle/viewport.
        # Treat that as a cancelled drag so rotation cannot continue running.
        if not (event.buttons() & Qt.LeftButton):
            if self.rotate_space_index is not None:
                self.rotate_space_index = None
                self.rotate_start_center = None
                self._commit_current_space(show_errors=False)
                self.refresh_scene()
            self.drag_point_index = None
            self.drag_whole_space = False
            self.drag_start_world = None
            self.drag_start_points = []
            self.drag_payload_index = None
            self.drag_payload_start_world = None
            self.drag_payload_start = None
            self.drag_space_indices = set()
            self.drag_spaces_start_world = None
            self.drag_spaces_start_slots = {}
            return

        if self.rotate_space_index is not None and self.rotate_start_center is not None:
            slot = self._space_payload_slot(self.rotate_space_index)
            if slot:
                cx, cy = self.rotate_start_center
                angle = math.degrees(
                    math.atan2(float(y) - float(cy), float(x) - float(cx))
                )
                if event.modifiers() & Qt.ShiftModifier:
                    angle = round(angle / 90.0) * 90.0
                slot["rotation_deg"] = self._normalise_degrees(angle)
                self.selected_space_index = self.rotate_space_index
                self.selected_payload_index = 0
                self._sync_space_from_payload_index(self.rotate_space_index)
                self.current_points = self._slot_polygon_points(slot)
                self.refresh_scene()
            return

        if self.drag_spaces_start_world is not None and self.drag_spaces_start_slots:
            dx = x - float(self.drag_spaces_start_world["x"])
            dy = y - float(self.drag_spaces_start_world["y"])
            self._move_space_group(self.drag_spaces_start_slots, dx, dy)
            if self.selected_space_index is not None:
                self._sync_space_from_payload_index(self.selected_space_index)
            self.refresh_scene()
            return

        if (
            self.drag_payload_index is not None
            and self.drag_payload_start_world is not None
            and self.drag_payload_start is not None
        ):
            slots = self._current_space_slots()
            if 0 <= self.drag_payload_index < len(slots):
                start_x, start_y = self._slot_center_absolute(self.drag_payload_start)
                dx = x - float(self.drag_payload_start_world["x"])
                dy = y - float(self.drag_payload_start_world["y"])
                candidate = dict(self.drag_payload_start)
                self._set_slot_center_absolute(candidate, start_x + dx, start_y + dy)
                # Payload footprints define the inventory space, so movement is
                # allowed and then the space polygon is regenerated from the slot.
                slots[self.drag_payload_index].update(candidate)
                self._sync_current_space_from_payload()
                self.refresh_scene()
            return

        if self.drag_whole_space and self.drag_start_world is not None:
            dx = round(x - float(self.drag_start_world["x"]), 3)
            dy = round(y - float(self.drag_start_world["y"]), 3)

            self.current_points = [
                {
                    "x": round(float(p["x"]) + dx, 3),
                    "y": round(float(p["y"]) + dy, 3),
                }
                for p in self.drag_start_points
            ]

            self.refresh_scene()
            return

        if self.drag_point_index is None:
            return

        if 0 <= self.drag_point_index < len(self.current_points):
            self.current_points[self.drag_point_index] = {
                "x": round(x, 3),
                "y": round(y, 3),
            }
            self._apply_rectangle_snap(self.drag_point_index)
            self.refresh_scene()

    def _mouse_release(self, event):
        if self.rotate_space_index is not None:
            self.rotate_space_index = None
            self.rotate_start_center = None
            self._commit_current_space(show_errors=False)
            self.refresh_scene()

        self.drag_point_index = None
        self.drag_whole_space = False
        self.drag_start_world = None
        self.drag_start_points = []
        self.drag_payload_index = None
        self.drag_payload_start_world = None
        self.drag_payload_start = None
        self.drag_space_indices = set()
        self.drag_spaces_start_world = None
        self.drag_spaces_start_slots = {}
        event.accept()

    def _location_box_scene_rect(self):
        location_box = self.store.get_location_bounding_box_points(self.location_name)

        if not location_box:
            return None

        pts = [self.world_to_scene(p["x"], p["y"]) for p in location_box]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]

        return QRectF(
            min(xs),
            min(ys),
            max(xs) - min(xs),
            max(ys) - min(ys),
        )

    def copy_selected_space(self):
        row = self.space_list.currentRow()
        if row < 0 or row >= len(self.spaces):
            return

        self.copied_space = dict(self.spaces[row])
        self.copied_space["name"] = self.spaces[row].get("name", "Inventory space")
        self.copied_space["points"] = [dict(p) for p in self.spaces[row].get("points", [])]
        self.copied_space["payload_slots"] = [
            dict(slot) for slot in self.spaces[row].get("payload_slots", [])
        ]

        self.status_label.setText(f"Copied {self.copied_space['name']}")

    def paste_copied_space(self):
        if not self.copied_space:
            return

        pasted = dict(self.copied_space)
        pasted["name"] = f"{self.copied_space.get('name', 'Inventory space')} copy"
        pasted["points"] = [dict(p) for p in self.copied_space.get("points", [])]
        pasted["payload_slots"] = [
            dict(slot) for slot in self.copied_space.get("payload_slots", [])
        ]

        # Small offset so pasted space is visible and selectable separately
        for p in pasted["points"]:
            p["dx"] = round(float(p.get("dx", 0.0)) + 0.25, 3)
            p["dy"] = round(float(p.get("dy", 0.0)) + 0.25, 3)
        for slot in pasted.get("payload_slots", []):
            slot["dx"] = round(float(slot.get("dx", 0.0)) + 0.25, 3)
            slot["dy"] = round(float(slot.get("dy", 0.0)) + 0.25, 3)

        self.spaces.append(pasted)
        self.selected_space_index = len(self.spaces) - 1
        self.selected_space_indices = {self.selected_space_index}
        self.refresh_list()
        self.space_list.setCurrentRow(self.selected_space_index)
        self.select_space(self.selected_space_index)
        self.refresh_scene()

    def _draw_dxf_background(self):
        if not self.location:
            return

        editor = getattr(self, "editor", None)
        if editor is None:
            return

        floor = int(self.location.get("floor", 0))

        if getattr(editor, "loaded_dxf_floor", None) != floor:
            editor.ensure_floor_dxf_loaded(floor)

        if getattr(editor, "loaded_dxf_floor", None) != floor:
            return

        dxf_scene = getattr(editor, "dxf_scene", None)
        if dxf_scene is None or not dxf_scene.entities:
            return

        view_rect = self._location_box_scene_rect()
        if view_rect is None or view_rect.isNull():
            return

        world_rect = QRectF(
            view_rect.left(),
            -view_rect.bottom(),
            view_rect.width(),
            view_rect.height(),
        ).adjusted(-1.0, -1.0, 1.0, 1.0)

        line_path = QPainterPath()
        poly_path = QPainterPath()
        arc_path = QPainterPath()

        for entity in dxf_scene.entities:
            bbox = entity.get("bbox")
            if bbox:
                min_x, min_y, max_x, max_y = bbox
                if (
                    max_x < world_rect.left()
                    or min_x > world_rect.right()
                    or max_y < world_rect.top()
                    or min_y > world_rect.bottom()
                ):
                    continue

            etype = entity.get("type")

            if etype == "LINE":
                x1, y1 = entity["start"]
                x2, y2 = entity["end"]
                line_path.moveTo(x1, -y1)
                line_path.lineTo(x2, -y2)

            elif etype == "POLYLINE":
                pts = [QPointF(x, -y) for x, y in entity.get("points", [])]
                if len(pts) >= 2:
                    poly_path.moveTo(pts[0])
                    for pt in pts[1:]:
                        poly_path.lineTo(pt)
                    if entity.get("closed"):
                        poly_path.closeSubpath()

            elif etype == "CIRCLE":
                cx, cy = entity["center"]
                r = float(entity["radius"])
                arc_path.addEllipse(QRectF(cx - r, -(cy + r), r * 2, r * 2))

            elif etype == "ARC":
                cx, cy = entity["center"]
                r = float(entity["radius"])
                start_angle = float(entity.get("start_angle", 0.0))
                end_angle = float(entity.get("end_angle", 0.0))
                span_angle = end_angle - start_angle
                if span_angle <= 0:
                    span_angle += 360.0

                rect = QRectF(cx - r, -(cy + r), r * 2, r * 2)
                arc_path.arcMoveTo(rect, -start_angle)
                arc_path.arcTo(rect, -start_angle, -span_angle)

        for path, colour in [
            (line_path, "#777777"),
            (poly_path, "#999999"),
            (arc_path, "#777777"),
        ]:
            if path.isEmpty():
                continue

            item = QGraphicsPathItem(path)
            item.setPen(QPen(QColor(colour), 0))
            item.setBrush(Qt.NoBrush)
            item.setZValue(-100)
            item.setOpacity(0.45)
            item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
            self.scene.addItem(item)

    def refresh_scene(self):
        self.scene.clear()
        self._draw_dxf_background()

        location_box = self.store.get_location_bounding_box_points(self.location_name)

        if location_box:
            pts = [self.world_to_scene(p["x"], p["y"]) for p in location_box]
            poly = QGraphicsPolygonItem(QPolygonF(pts))
            poly.setPen(QPen(QColor("#18c37e"), 0.0))
            poly.setBrush(QBrush(QColor(24, 195, 126, 18)))
            self.scene.addItem(poly)

        for idx, space in enumerate(self.spaces):
            if idx == self.selected_space_index:
                continue

            pts_abs = self.store.inventory_space_points_absolute(
                self.location_name,
                space,
            )
            if len(pts_abs) < 3:
                continue

            pts = [self.world_to_scene(p["x"], p["y"]) for p in pts_abs]
            poly = QGraphicsPolygonItem(QPolygonF(pts))
            is_multi_selected = idx in self.selected_space_indices
            poly.setPen(
                QPen(QColor("#ffffff" if is_multi_selected else "#6aa9ff"), 0.0)
            )
            poly.setBrush(
                QBrush(QColor(106, 169, 255, 38 if is_multi_selected else 18))
            )
            poly.setZValue(-10)
            self.scene.addItem(poly)

            label = QGraphicsSimpleTextItem(space.get("name", "Inventory"))
            label.setBrush(QBrush(QColor("#bcd7ff")))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setPos(pts[0])
            self.scene.addItem(label)

        if self.current_points:
            pts = [self.world_to_scene(p["x"], p["y"]) for p in self.current_points]

            if len(pts) >= 3:
                poly = QGraphicsPolygonItem(QPolygonF(pts))
                poly.setPen(QPen(QColor("#ffdd57"), 0.0))
                poly.setBrush(QBrush(QColor(255, 221, 87, 30)))
                self.scene.addItem(poly)

            # No freehand handles: inventory spaces are fixed payload footprints.

        for slot_index, slot in enumerate(self._current_space_slots()):
            item_name = self._slot_label(slot)
            poly_points = self._slot_polygon_points(slot)
            if len(poly_points) < 3:
                continue
            pts = [self.world_to_scene(p["x"], p["y"]) for p in poly_points]
            item = QGraphicsPolygonItem(QPolygonF(pts))
            selected = slot_index == self.selected_payload_index
            item.setPen(QPen(QColor("#ffffff" if selected else "#3da5ff"), 0.0))
            item.setBrush(QBrush(QColor(61, 165, 255, 65 if selected else 35)))
            item.setZValue(15)
            self.scene.addItem(item)
            cx, cy = self._slot_center_absolute(slot)
            label = QGraphicsSimpleTextItem(item_name)
            label.setBrush(QBrush(QColor("#e3f2ff")))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setPos(self.world_to_scene(cx, cy))
            label.setZValue(16)
            self.scene.addItem(label)

            if selected:
                handle = self._rotation_handle_world(slot)
                if handle:
                    centre_scene = self.world_to_scene(cx, cy)
                    handle_scene = self.world_to_scene(handle["x"], handle["y"])
                    line = self.scene.addLine(
                        centre_scene.x(),
                        centre_scene.y(),
                        handle_scene.x(),
                        handle_scene.y(),
                        QPen(QColor("#ffffff"), 0.0),
                    )
                    line.setZValue(17)
                    r = 0.12
                    ellipse = self.scene.addEllipse(
                        handle_scene.x() - r,
                        handle_scene.y() - r,
                        r * 2.0,
                        r * 2.0,
                        QPen(QColor("#ffffff"), 0.0),
                        QBrush(QColor("#ffdd57")),
                    )
                    ellipse.setZValue(18)

        base_rect = self._location_box_scene_rect()
        items_rect = self.scene.itemsBoundingRect()

        if base_rect is None or base_rect.isNull():
            base_rect = items_rect
        elif not items_rect.isNull():
            # Keep out-of-bound payload/AMR spaces inside the scrollable scene.
            # Without this, the view's scene rect stayed locked to the location
            # boundary and spaces outside it could be impossible to pan to, pick,
            # and drag back.
            base_rect = base_rect.united(items_rect)

        if not base_rect.isNull():
            padded = base_rect.adjusted(-2, -2, 2, 2)
            self.scene.setSceneRect(padded)

            if not self._initial_fit_done:
                self.view.fitInView(padded, Qt.KeepAspectRatio)
                self._initial_fit_done = True

        self._refresh_size_fields()
