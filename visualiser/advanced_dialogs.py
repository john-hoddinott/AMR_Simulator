import calendar
import json
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial


from PySide6.QtCore import QDate, QDateTime, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QColor, QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateTimeEdit,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
    QToolButton,
)


from ui_theme import polish_dialog as _polish_dialog
from models import normalise_delivery_resource_policy


DELIVERY_RESOURCE_OPTIONS = [
    ("AMR only", "amr"),
    ("Staff only", "staff"),
    ("AMR or staff", "either"),
]


def _delivery_resource_combo(value=None):
    combo = QComboBox()
    for label, mode in DELIVERY_RESOURCE_OPTIONS:
        combo.addItem(label, mode)
    mode = normalise_delivery_resource_policy(value).get("mode", "amr")
    index = combo.findData(mode)
    combo.setCurrentIndex(max(0, index))
    combo.setToolTip(
        "Select which delivery resource class may transport this payload. "
        "Existing configurations default to AMR only."
    )
    return combo


def _date_time_input(value=None):
    edit = QDateTimeEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
    parsed = QDateTime.fromString(str(value or ""), Qt.ISODate)
    if not parsed.isValid():
        parsed = QDateTime.currentDateTime()
    edit.setDateTime(parsed)
    return edit


def _integer_input(value=0, minimum=0, maximum=1_000_000_000, suffix=""):
    edit = QSpinBox()
    edit.setRange(int(minimum), int(maximum))
    try:
        numeric = int(float(value))
    except Exception:
        numeric = int(minimum)
    edit.setValue(max(int(minimum), min(int(maximum), numeric)))
    edit.setKeyboardTracking(False)
    edit.setAccelerated(True)
    if suffix:
        edit.setSuffix(str(suffix))
    return edit

def ask_string(parent, title, prompt, text=""):
    value, ok = QInputDialog.getText(parent, title, prompt, text=text)
    return value if ok else None


class TaskCellButton(QToolButton):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def ask_int(parent, title, prompt, value=0, minimum=-2147483648, maximum=2147483647):
    result, ok = QInputDialog.getInt(
        parent, title, prompt, value=value, minValue=minimum, maxValue=maximum
    )
    return result if ok else None


class MultiSelectPicker(QDialog):
    def __init__(self, parent, title, options, selected=None, group_resolver=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 620)
        self.result = None
        self.options = list(options)
        self.selected = set(selected or [])
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.checkboxes = {}
        self.visible = []

        layout = QVBoxLayout(self)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Search available items...")
        self.filter_edit.textChanged.connect(self.refresh)
        layout.addWidget(self.filter_edit)

        tools = QHBoxLayout()
        layout.addLayout(tools)
        for text, handler in [
            ("All", self.select_all),
            ("None", self.clear_all),
            ("Select visible", self.select_visible),
            ("Clear visible", self.clear_visible),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            tools.addWidget(btn)
        tools.addStretch(1)
        self.selection_summary = QLabel()
        self.selection_summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tools.addWidget(self.selection_summary)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.finish)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()
        _polish_dialog(self)

    def refresh(self):
        self._sync_visible_state()
        while self.container_layout.count():
            item = self.container_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        filter_text = self.filter_edit.text().strip().lower()
        self.visible = []
        grouped = {}
        for item in self.options:
            if filter_text and filter_text not in item.lower():
                continue
            grouped.setdefault(self.group_resolver(item), []).append(item)

        for group_name in sorted(grouped.keys(), key=str):
            items = sorted(grouped[group_name])

            header = QHBoxLayout()
            label = QLabel(f"{group_name} ({len(items)})")
            header.addWidget(label)
            header.addStretch(1)
            btn_all = QPushButton("All")
            btn_none = QPushButton("None")
            btn_all.setFixedWidth(52)
            btn_none.setFixedWidth(52)
            btn_all.clicked.connect(
                lambda _=False, its=items: self._set_items(its, True)
            )
            btn_none.clicked.connect(
                lambda _=False, its=items: self._set_items(its, False)
            )
            header.addWidget(btn_all)
            header.addWidget(btn_none)

            header_widget = QWidget()
            header_widget.setLayout(header)
            self.container_layout.addWidget(header_widget)

            for item in items:
                checked = item in self.selected
                existing = self.checkboxes.get(item)
                if existing is not None:
                    try:
                        checked = existing.isChecked()
                    except RuntimeError:
                        pass

                chk = QCheckBox(item)
                chk.setChecked(checked)
                chk.toggled.connect(self._update_selection_summary)
                self.checkboxes[item] = chk

                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(18, 0, 0, 0)
                row_layout.addWidget(chk)
                row_layout.addStretch(1)
                self.container_layout.addWidget(row)
                self.visible.append(item)

        self.container_layout.addStretch(1)
        self._update_selection_summary()

    def _update_selection_summary(self):
        selected = set(self.selected)
        for name, check in self.checkboxes.items():
            try:
                if check.isChecked():
                    selected.add(name)
                else:
                    selected.discard(name)
            except RuntimeError:
                continue
        visible_selected = sum(1 for name in self.visible if name in selected)
        self.selection_summary.setText(
            f"{len(selected)} selected" + (f" · {visible_selected} visible" if self.filter_edit.text().strip() else "")
        )

    def _set_items(self, items, value):
        for name in items:
            if value:
                self.selected.add(name)
            else:
                self.selected.discard(name)

            chk = self.checkboxes.get(name)
            if chk is not None:
                try:
                    chk.setChecked(value)
                except RuntimeError:
                    pass
        self._update_selection_summary()

    def select_all(self):
        self._set_items(self.options, True)

    def clear_all(self):
        self._set_items(self.options, False)

    def select_visible(self):
        self._set_items(self.visible, True)

    def clear_visible(self):
        self._set_items(self.visible, False)

    def finish(self):
        result = []
        for item in self.options:
            chk = self.checkboxes.get(item)
            checked = item in self.selected
            if chk is not None:
                try:
                    checked = chk.isChecked()
                except RuntimeError:
                    pass
            if checked:
                result.append(item)

        self.result = result
        self.selected = set(result)
        self.accept()

    def _sync_visible_state(self):
        for item, chk in self.checkboxes.items():
            try:
                if chk.isChecked():
                    self.selected.add(item)
                else:
                    self.selected.discard(item)
            except RuntimeError:
                continue


class RouteProfilesEditorV2(QDialog):
    def __init__(
        self,
        master,
        profiles,
        point_names,
        lift_ids,
        corridor_edges,
        on_save,
        floor_map=None,
        manual_single_payload_available=True,
    ):
        self.editor = master
        super().__init__(master)
        self.setWindowTitle("Route Profiles")
        self.resize(1200, 720)
        self.profiles = json.loads(json.dumps(profiles))
        self.point_names = sorted(point_names)
        self.lift_ids = sorted(lift_ids)
        self.corridor_edges = corridor_edges
        self.floor_map = floor_map or {}
        self.on_save = on_save
        self.current_profile = None
        self.allowed_lifts = []
        self.allowed_nodes = []

        root_layout = QVBoxLayout(self)
        layout = QHBoxLayout()
        root_layout.addLayout(layout, 1)
        splitter = QSplitter()
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.profile_list = QListWidget()
        self.profile_list.currentTextChanged.connect(self.on_profile_select)
        left_layout.addWidget(self.profile_list)
        btn_row = QHBoxLayout()
        left_layout.addLayout(btn_row)
        add_btn = QPushButton("Add")
        del_btn = QPushButton("Delete")
        add_btn.clicked.connect(self.add_profile)
        del_btn.clicked.connect(self.delete_profile)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)

        right = QWidget()
        form = QVBoxLayout(right)
        profile_form = QFormLayout()
        form.addLayout(profile_form)
        self.name_edit = QLineEdit()
        profile_form.addRow("Profile name", self.name_edit)

        lifts_row = QHBoxLayout()
        self.lifts_summary = QLabel("None")
        pick_lifts = QPushButton("Pick")
        pick_lifts.clicked.connect(self.pick_lifts)
        lifts_row.addWidget(self.lifts_summary, 1)
        lifts_row.addWidget(pick_lifts)
        profile_form.addRow("Allowed lifts", lifts_row)

        nodes_row = QHBoxLayout()
        self.nodes_summary = QLabel("None")
        self.nodes_summary.setWordWrap(True)
        pick_nodes = QPushButton("Pick")
        pick_nodes.clicked.connect(self.pick_nodes)
        nodes_row.addWidget(self.nodes_summary, 1)
        nodes_row.addWidget(pick_nodes)
        profile_form.addRow("Allowed nodes", nodes_row)

        graphical_nodes_btn = QPushButton("Graphical...")
        graphical_nodes_btn.clicked.connect(self.pick_nodes_graphically)
        nodes_row.addWidget(graphical_nodes_btn)

        form.addWidget(QLabel("Allowed edges as JSON array pairs"))
        self.edges_text = QPlainTextEdit()
        form.addWidget(self.edges_text, 1)

        edge_row = QHBoxLayout()
        gen_btn = QPushButton("Generate from selected nodes")
        clr_btn = QPushButton("Clear edges")
        gen_btn.clicked.connect(self.fill_edges_from_nodes)
        clr_btn.clicked.connect(lambda: self.edges_text.setPlainText(""))
        edge_row.addWidget(gen_btn)
        edge_row.addWidget(clr_btn)
        edge_row.addStretch(1)
        form.addLayout(edge_row)

        lower = QHBoxLayout()
        apply_btn = QPushButton("Apply Changes")
        save_btn = QPushButton("Save All")
        apply_btn.clicked.connect(self.apply_profile_changes)
        save_btn.clicked.connect(self.save_all)
        lower.addWidget(apply_btn)
        lower.addStretch(1)
        lower.addWidget(save_btn)
        form.addLayout(lower)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        for name in self.profiles.keys():
            self.profile_list.addItem(name)
        if self.profiles:
            self.profile_list.setCurrentRow(0)
        _polish_dialog(self)

    def summarize(self, values):
        if not values:
            return "None"
        if len(values) <= 6:
            return ", ".join(values)
        return f"{len(values)} selected"

    def _group_for_item(self, item):
        floor = self.floor_map.get(item)
        return f"Floor {floor}" if floor is not None else "Other"

    def add_profile(self):
        name = ask_string(self, "New profile", "Profile name:")
        if not name:
            return
        if name in self.profiles:
            QMessageBox.critical(self, "Duplicate", "Profile already exists")
            return
        self.profiles[name] = {
            "allowed_lifts": [],
            "allowed_nodes": [],
            "allowed_edges": [],
        }
        self.profile_list.addItem(name)
        items = self.profile_list.findItems(name, Qt.MatchExactly)
        if items:
            self.profile_list.setCurrentItem(items[0])

    def delete_profile(self):
        item = self.profile_list.currentItem()
        if item is None:
            return
        name = item.text()
        if name == "default":
            QMessageBox.critical(self, "Not allowed", "Cannot delete default profile")
            return
        del self.profiles[name]
        row = self.profile_list.row(item)
        self.profile_list.takeItem(row)
        self.current_profile = None
        self.name_edit.clear()
        self.lifts_summary.setText("None")
        self.nodes_summary.setText("None")
        self.edges_text.setPlainText("")

    def pick_lifts(self):
        picker = MultiSelectPicker(
            self,
            "Pick lifts",
            self.lift_ids,
            self.allowed_lifts,
            group_resolver=self._group_for_item,
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.allowed_lifts = sorted(picker.result)
            self.lifts_summary.setText(self.summarize(self.allowed_lifts))

    def pick_nodes(self):
        picker = MultiSelectPicker(
            self,
            "Pick nodes",
            self.point_names,
            self.allowed_nodes,
            group_resolver=self._group_for_item,
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.allowed_nodes = sorted(picker.result)
            self.nodes_summary.setText(self.summarize(self.allowed_nodes))

    def fill_edges_from_nodes(self):
        allowed = set(self.allowed_nodes)
        profile_edges = [
            [e["from"], e["to"]]
            for e in self.corridor_edges
            if e["from"] in allowed and e["to"] in allowed
        ]
        self.edges_text.setPlainText(json.dumps(profile_edges, indent=2))

    def on_profile_select(self, name):
        if not name:
            return
        self.current_profile = name
        profile = self.profiles[name]
        self.name_edit.setText(name)
        self.allowed_lifts = list(profile.get("allowed_lifts", []))
        self.allowed_nodes = list(profile.get("allowed_nodes", []))
        self.lifts_summary.setText(self.summarize(self.allowed_lifts))
        self.nodes_summary.setText(self.summarize(self.allowed_nodes))
        self.edges_text.setPlainText(
            json.dumps(profile.get("allowed_edges", []), indent=2)
        )

    def apply_profile_changes(self):
        if not self.current_profile:
            return
        try:
            new_name = self.name_edit.text().strip()
            if not new_name:
                raise ValueError("Profile name is required")
            edges = json.loads(self.edges_text.toPlainText().strip() or "[]")
            if not isinstance(edges, list):
                raise ValueError("Allowed edges must be a JSON list")
            payload = {
                "allowed_lifts": list(self.allowed_lifts),
                "allowed_nodes": list(self.allowed_nodes),
                "allowed_edges": edges,
            }
            if new_name != self.current_profile:
                self.profiles[new_name] = payload
                del self.profiles[self.current_profile]
                self.profile_list.currentItem().setText(new_name)
                self.current_profile = new_name
            else:
                self.profiles[self.current_profile] = payload
        except Exception as exc:
            QMessageBox.critical(self, "Invalid profile", str(exc))

    def save_all(self):
        self.apply_profile_changes()
        self.on_save(self.profiles)
        self.accept()

    def pick_nodes_graphically(self):
        if not hasattr(self.editor, "start_route_profile_graphical_selection"):
            QMessageBox.critical(
                self,
                "Graphical selection unavailable",
                "The main editor does not support graphical route profile selection.",
            )
            return

        self.hide()

        self.editor.start_route_profile_graphical_selection(
            allowed_point_names=set(self.point_names),
            selected_nodes=set(self.allowed_nodes),
            callback=self.apply_graphical_node_selection,
            return_window=self,
        )

    def apply_graphical_node_selection(self, selected_nodes):
        self.allowed_nodes = sorted(selected_nodes)
        self.nodes_summary.setText(self.summarize(self.allowed_nodes))
        self.fill_edges_from_nodes()
        self.show()
        self.raise_()
        self.activateWindow()


class TaskFormDialog(QDialog):
    def __init__(
        self,
        parent,
        location_names,
        payload_names,
        profile_names,
        seed=None,
        default_task_id="T1",
        group_resolver=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Task")
        self.location_names = list(location_names)
        self.payload_names = list(payload_names)
        self.profile_names = list(profile_names)
        self.seed = seed or {}
        self.default_task_id = default_task_id
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.result = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.id_edit = QLineEdit(self.seed.get("id", self.default_task_id))

        pickup_row = QHBoxLayout()
        self.pickup_edit = QLineEdit(self.seed.get("pickup", ""))
        self.pickup_edit.setReadOnly(True)
        pickup_btn = QPushButton("Select...")
        pickup_btn.clicked.connect(self._pick_pickup)
        pickup_row.addWidget(self.pickup_edit)
        pickup_row.addWidget(pickup_btn)

        dropoff_row = QHBoxLayout()
        self.dropoff_edit = QLineEdit(self.seed.get("dropoff", ""))
        self.dropoff_edit.setReadOnly(True)
        dropoff_btn = QPushButton("Select...")
        dropoff_btn.clicked.connect(self._pick_dropoff)
        dropoff_row.addWidget(self.dropoff_edit)
        dropoff_row.addWidget(dropoff_btn)

        self.payload_combo = QComboBox()
        self.payload_combo.addItems(self.payload_names)
        self.payload_combo.setCurrentText(self.seed.get("payload", ""))
        self.release_edit = _date_time_input(
            self.seed.get("release_datetime", "2026-01-01T08:00:00")
        )
        self.target_edit = _integer_input(
            self.seed.get("target_time", 300), minimum=0, maximum=31_536_000, suffix=" s"
        )
        self.target_edit.setToolTip("Target journey or completion allowance in seconds.")
        self.priority_edit = _integer_input(
            self.seed.get("priority", 10), minimum=0, maximum=999_999
        )
        self.priority_edit.setToolTip("Lower or higher values are interpreted using the existing simulator priority rules.")
        self.labels_edit = QLineEdit(", ".join(self.seed.get("labels", [""])))
        self.route_profile_combo = QComboBox()
        self.route_profile_combo.addItems(self.profile_names)
        self.route_profile_combo.setCurrentText(self.seed.get("route_profile", ""))
        self.delivery_resource_combo = _delivery_resource_combo(
            self.seed.get("delivery_resource", self.seed.get("delivery_method", "amr"))
        )

        form.addRow("ID", self.id_edit)
        form.addRow("Pickup", pickup_row)
        form.addRow("Dropoff", dropoff_row)
        form.addRow("Payload", self.payload_combo)
        form.addRow("Release date and time", self.release_edit)
        form.addRow("Target allowance", self.target_edit)
        form.addRow("Priority", self.priority_edit)
        form.addRow("Labels (comma-separated)", self.labels_edit)
        form.addRow("Route profile", self.route_profile_combo)
        form.addRow("Delivery resource", self.delivery_resource_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 360)
        _polish_dialog(self)

    def _pick_single_location(self, title, current_text):
        picker = MultiSelectPicker(
            self,
            title,
            self.location_names,
            selected=[current_text] if current_text else [],
            group_resolver=self.group_resolver,
        )
        if picker.exec() == QDialog.Accepted and picker.result:
            return picker.result[0]
        return None

    def _pick_pickup(self):
        value = self._pick_single_location("Select pickup", self.pickup_edit.text())
        if value:
            self.pickup_edit.setText(value)

    def _pick_dropoff(self):
        value = self._pick_single_location("Select dropoff", self.dropoff_edit.text())
        if value:
            self.dropoff_edit.setText(value)

    def accept(self):
        try:
            if not self.id_edit.text().strip():
                raise ValueError("ID is required")
            if not self.pickup_edit.text().strip():
                raise ValueError("Pickup is required")
            if not self.dropoff_edit.text().strip():
                raise ValueError("Dropoff is required")
            if not self.payload_combo.currentText().strip():
                raise ValueError("Payload is required")
            self.result = {
                "id": self.id_edit.text().strip(),
                "pickup": self.pickup_edit.text().strip(),
                "dropoff": self.dropoff_edit.text().strip(),
                "payload": self.payload_combo.currentText().strip(),
                "release_datetime": self.release_edit.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss"),
                "target_time": int(self.target_edit.value()),
                "priority": int(self.priority_edit.value()),
                "labels": [x.strip() for x in self.labels_edit.text().split(",")],
                "route_profile": self.route_profile_combo.currentText().strip(),
                "delivery_resource": normalise_delivery_resource_policy(
                    {"mode": self.delivery_resource_combo.currentData()}
                ),
                "manual_task": True,
                "manual_single_payload_only": True,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid task", str(exc))


class BulkOneToManyTaskDialog(QDialog):
    def __init__(
        self,
        parent,
        location_names,
        payload_names,
        profile_names,
        group_resolver=None,
        default_task_id="T1",
    ):
        super().__init__(parent)
        self.setWindowTitle("Create One-to-Many Tasks")
        self.location_names = sorted(location_names)
        self.payload_names = sorted(payload_names)
        self.profile_names = list(profile_names)
        self.group_resolver = group_resolver or (lambda item: "Other")
        self.default_task_id = default_task_id
        self.selected_dropoffs = []
        self.result = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.id_edit = QLineEdit(self.default_task_id)
        self.pickup_combo = QComboBox()
        self.pickup_combo.addItems(self.location_names)

        dropoff_row = QHBoxLayout()
        self.dropoff_summary = QLabel("None selected")
        self.dropoff_summary.setWordWrap(True)
        pick_dropoffs = QPushButton("Select...")
        pick_dropoffs.clicked.connect(self._pick_dropoffs)
        dropoff_row.addWidget(self.dropoff_summary, 1)
        dropoff_row.addWidget(pick_dropoffs)

        self.payload_combo = QComboBox()
        self.payload_combo.addItems(self.payload_names)
        self.release_edit = _date_time_input("2026-01-01T08:00:00")
        self.target_edit = _integer_input(300, minimum=0, maximum=31_536_000, suffix=" s")
        self.priority_edit = _integer_input(10, minimum=0, maximum=999_999)
        self.labels_edit = QLineEdit("")
        self.route_combo = QComboBox()
        self.route_combo.addItems(self.profile_names)
        self.delivery_resource_combo = _delivery_resource_combo("amr")

        form.addRow("Base task ID", self.id_edit)
        form.addRow("Pickup", self.pickup_combo)
        form.addRow("Dropoffs", dropoff_row)
        form.addRow("Payload", self.payload_combo)
        form.addRow("Release date and time", self.release_edit)
        form.addRow("Target allowance", self.target_edit)
        form.addRow("Priority", self.priority_edit)
        form.addRow("Labels (comma-separated)", self.labels_edit)
        form.addRow("Route profile", self.route_combo)
        form.addRow("Delivery resource", self.delivery_resource_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        _polish_dialog(self)

    def _pick_dropoffs(self):
        picker = MultiSelectPicker(
            self,
            "Select dropoffs",
            self.location_names,
            selected=self.selected_dropoffs,
            group_resolver=self.group_resolver,
        )
        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.selected_dropoffs = sorted(picker.result)
            if not self.selected_dropoffs:
                self.dropoff_summary.setText("None selected")
            elif len(self.selected_dropoffs) <= 4:
                self.dropoff_summary.setText(", ".join(self.selected_dropoffs))
            else:
                self.dropoff_summary.setText(f"{len(self.selected_dropoffs)} selected")

    def accept(self):
        try:
            if not self.id_edit.text().strip():
                raise ValueError("Base task ID is required")
            if not self.pickup_combo.currentText().strip():
                raise ValueError("Pickup is required")
            if not self.payload_combo.currentText().strip():
                raise ValueError("Payload is required")
            if not self.selected_dropoffs:
                raise ValueError("Select at least one dropoff")
            if self.pickup_combo.currentText().strip() in self.selected_dropoffs:
                raise ValueError("Pickup cannot also be a dropoff")
            labels = (
                [x.strip() for x in self.labels_edit.text().split(",")]
                if self.labels_edit.text().strip()
                else [""]
            )
            self.result = {
                "base_id": self.id_edit.text().strip(),
                "pickup": self.pickup_combo.currentText().strip(),
                "dropoffs": list(self.selected_dropoffs),
                "payload": self.payload_combo.currentText().strip(),
                "release_datetime": self.release_edit.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss"),
                "target_time": int(self.target_edit.value()),
                "priority": int(self.priority_edit.value()),
                "labels": labels,
                "route_profile": self.route_combo.currentText().strip(),
                "delivery_resource": normalise_delivery_resource_policy(
                    {"mode": self.delivery_resource_combo.currentData()}
                ),
                "manual_task": True,
                "manual_single_payload_only": True,
            }
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid bulk task", str(exc))


class MultiDaySelectDialog(QDialog):
    def __init__(self, parent, initial_date=None):
        super().__init__(parent)
        self.setWindowTitle("Select target days")
        self.resize(820, 560)
        base = initial_date or datetime.now()
        self.display_year = base.year
        self.display_month = base.month
        self.selected_dates = set()
        self.result = None
        self.last_clicked_date = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        layout.addLayout(header)
        for text, handler in [
            ("◀", self.prev_month),
            ("Today", self.go_to_today),
            ("▶", self.next_month),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            header.addWidget(btn)
        self.title_label = QLabel()
        header.addWidget(self.title_label)
        header.addStretch(1)
        month_btn = QPushButton("Select displayed month")
        clear_btn = QPushButton("Clear all")
        month_btn.clicked.connect(self.select_displayed_month)
        clear_btn.clicked.connect(self.clear_selection)
        header.addWidget(clear_btn)
        header.addWidget(month_btn)

        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        layout.addWidget(self.calendar, 1)
        self.calendar.clicked.connect(self.on_day_clicked)

        footer = QHBoxLayout()
        layout.addLayout(footer)
        self.summary_label = QLabel("No days selected")
        footer.addWidget(self.summary_label)
        footer.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.finish)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)

        self.refresh()
        _polish_dialog(self)

    def prev_month(self):
        if self.display_month == 1:
            self.display_month = 12
            self.display_year -= 1
        else:
            self.display_month -= 1
        self.refresh()

    def next_month(self):
        if self.display_month == 12:
            self.display_month = 1
            self.display_year += 1
        else:
            self.display_month += 1
        self.refresh()

    def go_to_today(self):
        today = datetime.now()
        self.display_year = today.year
        self.display_month = today.month
        self.refresh()

    def clear_selection(self):
        self.selected_dates.clear()
        self.refresh()

    def select_displayed_month(self):
        cal = calendar.Calendar(firstweekday=0)
        for dt in cal.itermonthdates(self.display_year, self.display_month):
            if dt.month == self.display_month:
                self.selected_dates.add(dt.isoformat())
        self.refresh()

    def on_day_clicked(self, qdate):
        date_obj = datetime(qdate.year(), qdate.month(), qdate.day()).date()
        extend_range = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        key = date_obj.isoformat()
        if extend_range and self.last_clicked_date is not None:
            start_date = min(self.last_clicked_date, date_obj)
            end_date = max(self.last_clicked_date, date_obj)
            current = start_date
            while current <= end_date:
                self.selected_dates.add(current.isoformat())
                current = current + timedelta(days=1)
        else:
            if key in self.selected_dates:
                self.selected_dates.remove(key)
            else:
                self.selected_dates.add(key)
        self.last_clicked_date = date_obj
        self.refresh()

    def refresh(self):
        self.title_label.setText(
            f"{calendar.month_name[self.display_month]} {self.display_year}"
        )
        self.calendar.setSelectedDate(QDate(self.display_year, self.display_month, 1))
        count = len(self.selected_dates)
        if count == 0:
            self.summary_label.setText("No days selected")
        elif count <= 6:
            self.summary_label.setText(", ".join(sorted(self.selected_dates)))
        else:
            self.summary_label.setText(f"{count} days selected")

    def finish(self):
        self.result = sorted(self.selected_dates)
        self.accept()


class TaskPlannerDialog(QMainWindow):
    def __init__(
        self,
        master,
        items,
        location_names,
        payload_names,
        profile_names,
        suggest_task_id,
        on_save,
        floor_map=None,
        manual_single_payload_available=True,
    ):
        super().__init__(master)
        self.setWindowTitle("Task Planner")
        self.resize(1450, 760)
        self.items = items
        self.location_names = sorted(location_names)
        self.floor_map = floor_map or {}
        self.grouped_rows = self._build_grouped_rows()
        self.payload_names = payload_names
        self.profile_names = profile_names
        self.suggest_task_id = suggest_task_id
        self.on_save = on_save
        self.manual_single_payload_available = bool(manual_single_payload_available)
        self.day_start = self._initial_day()
        self.selected_task_index = None
        self.selected_row_name = None
        self.copied_task = None
        self._context_row_name = None
        self._context_task_index = None
        self._context_datetime = None
        self.task_fill_palette = [
            "#2e7d32",
            "#1976d2",
            "#f57c00",
            "#7b1fa2",
            "#c2185b",
            "#00838f",
            "#6d4c41",
            "#455a64",
        ]

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        layout.addLayout(toolbar)
        for text, handler in [
            ("◀ Day", lambda: self.shift_day(-1)),
            ("Today", self.go_to_today),
            ("Day ▶", lambda: self.shift_day(1)),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            toolbar.addWidget(btn)
        copy_day_btn = QPushButton("Copy Day...")
        copy_day_btn.clicked.connect(self.copy_day_tasks_to_other_day)
        toolbar.addWidget(copy_day_btn)
        toolbar.addWidget(self._make_vline())
        copy_btn = QPushButton("Copy")
        paste_btn = QPushButton("Paste")
        del_btn = QPushButton("Delete")
        copy_btn.clicked.connect(self.copy_selected_task)
        paste_btn.clicked.connect(self.paste_to_selected_row)
        del_btn.clicked.connect(self.delete_selected_task)
        toolbar.addWidget(copy_btn)
        toolbar.addWidget(paste_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch(1)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save)
        toolbar.addWidget(save_btn)
        self.date_label = QLabel()
        toolbar.addWidget(self.date_label)

        self.table = QTableWidget(0, 25)
        headers = ["Departments / drop-off"] + [f"{h:02d}:00" for h in range(24)]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.horizontalHeader().setDefaultSectionSize(90)
        self.table.setColumnWidth(0, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        self.table.cellClicked.connect(self.on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("Double-click a cell to create a task.")
        layout.addWidget(self.status_label)

        self.refresh_matrix()
        self.show()
        _polish_dialog(self)

    def _make_vline(self):
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _build_grouped_rows(self):
        grouped = {}
        for name in self.location_names:
            floor = self.floor_map.get(name)
            key = f"Floor {floor}" if floor is not None else "Other"
            grouped.setdefault(key, []).append(name)
        ordered = []
        for floor in sorted(grouped.keys()):
            ordered.append(("header", floor))
            for loc in sorted(grouped[floor]):
                ordered.append(("row", loc))
        return ordered

    def _initial_day(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for task in self.items:
            dt = self._task_datetime(task)
            if dt is not None:
                return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return today

    def _task_datetime(self, task):
        try:
            return datetime.fromisoformat(str(task.get("release_datetime", "")).strip())
        except Exception:
            return None

    def _next_task_id(self, reserved_ids=None):
        reserved_ids = set(reserved_ids or [])
        nums = []
        for task in self.items:
            task_id = str(task.get("id", ""))
            if task_id.startswith("T") and task_id[1:].isdigit():
                nums.append(int(task_id[1:]))
        for task_id in reserved_ids:
            task_id = str(task_id)
            if task_id.startswith("T") and task_id[1:].isdigit():
                nums.append(int(task_id[1:]))
        return f"T{max(nums, default=0) + 1}"

    def _format_cell_datetime(self, dt):
        return dt.replace(second=0, microsecond=0).isoformat(timespec="seconds")

    def _snap_to_grid(self, dt):
        minute = (dt.minute // 15) * 15
        return dt.replace(minute=minute, second=0, microsecond=0)

    def _task_slot_key(self, dt):
        return dt.replace(minute=0, second=0, microsecond=0)

    def _make_task_cell_widget(self, row_name, slot_dt, task_entries):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        for lane_index, (task_index, task) in enumerate(task_entries):
            btn = TaskCellButton(container)
            btn.setText(f"{task.get('id', '')}  {task.get('pickup', '')}")
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setMinimumHeight(22)
            btn.setStyleSheet(f"""
                QToolButton {{
                    background: {self.task_fill_palette[lane_index % len(self.task_fill_palette)]};
                    color: white;
                    border: 1px solid rgba(255,255,255,0.18);
                    border-radius: 4px;
                    padding: 3px 6px;
                    text-align: left;
                }}
                QToolButton:hover {{
                    border: 1px solid white;
                }}
                """)
            btn.clicked.connect(
                partial(self._select_task_from_cell, task_index, row_name)
            )
            btn.doubleClicked.connect(
                partial(self._edit_task_from_cell, task_index, row_name)
            )
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                partial(
                    self._show_task_button_context_menu,
                    task_index,
                    row_name,
                    slot_dt,
                    btn,
                )
            )
            layout.addWidget(btn)

        layout.addStretch(1)
        return container

    def _edit_task_from_cell(self, task_index, row_name):
        if task_index is None or task_index < 0 or task_index >= len(self.items):
            return

        self.selected_task_index = task_index
        self.selected_row_name = row_name

        dialog = TaskFormDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            seed=deepcopy(self.items[task_index]),
            default_task_id=self.items[task_index].get("id", self._next_task_id()),
            group_resolver=self._group_for_location,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.items[task_index] = dialog.result
            self.selected_task_index = task_index
            self.selected_row_name = dialog.result.get("dropoff", "")
            self.status_label.setText(f"Updated {dialog.result.get('id', '')}")
            self.refresh_matrix()

    def _select_task_from_cell(self, task_index, row_name):
        self.selected_task_index = task_index
        self.selected_row_name = row_name
        task = self.items[task_index]
        self.status_label.setText(
            f"Selected {task.get('id', '')} → {task.get('dropoff', '')}"
        )

    def _show_task_button_context_menu(self, task_index, row_name, when, button, pos):
        self.selected_task_index = task_index
        self.selected_row_name = row_name
        self._context_task_index = task_index
        self._context_row_name = row_name
        self._context_datetime = when

        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        delete_action = menu.addAction("Delete")
        action = menu.exec(button.mapToGlobal(pos))
        if action == copy_action:
            self.copy_selected_task()
        elif action == delete_action:
            self.delete_selected_task()

    def _group_for_location(self, item):
        floor = getattr(self, "floor_map", {}).get(item)
        return f"Floor {floor}" if floor is not None else "Other"

    def shift_day(self, days):
        self.day_start = self.day_start + timedelta(days=days)
        self.refresh_matrix()

    def go_to_today(self):
        self.day_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.refresh_matrix()

    def save(self):
        self.on_save(self.items)
        self.status_label.setText("Tasks saved")

    def _visible_tasks_for_day(self):
        results = []
        for idx, task in enumerate(self.items):
            dt = self._task_datetime(task)
            if dt is None:
                continue
            if self.day_start <= dt < (self.day_start + timedelta(days=1)):
                results.append((idx, task))
        return results

    def refresh_matrix(self):
        self.date_label.setText(self.day_start.strftime("%A %d %B %Y"))
        self.table.clearSpans()
        self.table.clearContents()

        row_count = len(self.grouped_rows)
        self.table.setRowCount(row_count)
        row_map = {}

        for row, (kind, name) in enumerate(self.grouped_rows):
            row_map[name] = row
            label_item = QTableWidgetItem(name)

            if kind == "header":
                label_item.setBackground(QBrush(QColor("#d0d7e5")))
                label_item.setFlags(Qt.ItemIsEnabled)
                self.table.setSpan(row, 0, 1, 25)
                self.table.setItem(row, 0, label_item)
                self.table.setRowHeight(row, 28)
                continue

            self.table.setItem(row, 0, label_item)
            self.table.setRowHeight(row, 38)

            for col in range(1, 25):
                placeholder = QTableWidgetItem("")
                placeholder.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(row, col, placeholder)
                self.table.removeCellWidget(row, col)

        self._row_map = row_map
        self._task_cell_lookup = {}
        self._draw_task_blocks()

    def _draw_task_blocks(self):
        tasks_by_cell = {}
        row_max_stacks = {}

        for idx, task in self._visible_tasks_for_day():
            dt = self._task_datetime(task)
            if dt is None:
                continue

            row_name = str(task.get("dropoff", "")).strip()
            if not row_name:
                continue

            row = self._row_map.get(row_name)
            if row is None:
                continue

            slot_dt = self._task_slot_key(dt)
            col = min(24, max(1, slot_dt.hour + 1))
            key = (row, col, row_name, slot_dt)
            tasks_by_cell.setdefault(key, []).append((idx, task))

        for (row, col, row_name, slot_dt), task_entries in tasks_by_cell.items():
            task_entries.sort(
                key=lambda item: (
                    self._task_datetime(item[1]) or self.day_start,
                    str(item[1].get("id", "")),
                )
            )

            self._task_cell_lookup[(row, col)] = [
                task_index for task_index, _task in task_entries
            ]
            row_max_stacks[row] = max(row_max_stacks.get(row, 1), len(task_entries))

            widget = self._make_task_cell_widget(row_name, slot_dt, task_entries)
            self.table.setCellWidget(row, col, widget)

        for row, stack_count in row_max_stacks.items():
            self.table.setRowHeight(row, max(38, 8 + (24 * stack_count)))

    def on_cell_clicked(self, row, col):
        self.selected_row_name = (
            self.grouped_rows[row][1] if self.grouped_rows[row][0] == "row" else None
        )

        task_indexes = self._task_cell_lookup.get((row, col), [])
        self.selected_task_index = task_indexes[0] if task_indexes else None

        if self.selected_task_index is not None:
            task = self.items[self.selected_task_index]
            self.status_label.setText(
                f"Selected {task.get('id', '')} → {task.get('dropoff', '')}"
            )
        elif self.selected_row_name:
            self.status_label.setText(
                f"Selected destination row: {self.selected_row_name}"
            )

    def _can_create_manual_task(self):
        if self.manual_single_payload_available:
            return True
        QMessageBox.critical(
            self,
            "Manual task unavailable",
            "Manual task creation requires a staff delivery type or an AMR with exactly one payload slot. Multi-stop AMRs are reserved for simulator route batching.",
        )
        return False

    def on_cell_double_clicked(self, row, col):
        if self.grouped_rows[row][0] != "row":
            return

        task_indexes = self._task_cell_lookup.get((row, col), [])
        task_index = task_indexes[0] if task_indexes else None
        row_name = self.grouped_rows[row][1]
        when = self._snap_to_grid(self.day_start + timedelta(hours=max(0, col - 1)))

        if task_index is not None:
            dialog = TaskFormDialog(
                self,
                self.location_names,
                self.payload_names,
                self.profile_names,
                seed=deepcopy(self.items[task_index]),
                default_task_id=self.items[task_index].get("id", self._next_task_id()),
                group_resolver=self._group_for_location,
            )
            if dialog.exec() == QDialog.Accepted and dialog.result:
                self.items[task_index] = dialog.result
                self.selected_task_index = task_index
                self.selected_row_name = dialog.result.get("dropoff", "")
                self.status_label.setText(f"Updated {dialog.result.get('id', '')}")
                self.refresh_matrix()
            return

        if not self._can_create_manual_task():
            return

        seed = {
            "dropoff": row_name,
            "release_datetime": self._format_cell_datetime(when),
        }
        dialog = TaskFormDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            seed=seed,
            default_task_id=self._next_task_id(),
            group_resolver=self._group_for_location,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.items.append(dialog.result)
            self.selected_task_index = len(self.items) - 1
            self.selected_row_name = dialog.result.get("dropoff", "")
            self.status_label.setText(f"Created {dialog.result.get('id', '')}")
            self.refresh_matrix()

    def on_context_menu(self, pos: QPoint):
        item = self.table.itemAt(pos)
        if item is None:
            return

        row = item.row()
        col = item.column()

        self._context_row_name = (
            self.grouped_rows[row][1] if self.grouped_rows[row][0] == "row" else None
        )
        task_indexes = self._task_cell_lookup.get((row, col), [])
        self._context_task_index = task_indexes[0] if task_indexes else None
        self._context_datetime = self._snap_to_grid(
            self.day_start + timedelta(hours=max(0, col - 1))
        )

        menu = QMenu(self)

        if self._context_task_index is not None:
            self.selected_task_index = self._context_task_index
            copy_action = menu.addAction("Copy")
            delete_action = menu.addAction("Delete")
            action = menu.exec(self.table.viewport().mapToGlobal(pos))
            if action == copy_action:
                self.copy_selected_task()
            elif action == delete_action:
                self.delete_selected_task()
        else:
            create_action = None
            paste_action = None
            if self._context_row_name:
                create_action = menu.addAction(
                    f"Create task at {self._context_row_name}"
                )
            if self.copied_task and self._context_row_name:
                paste_action = menu.addAction(f"Paste to {self._context_row_name}")

            action = menu.exec(self.table.viewport().mapToGlobal(pos))
            if action == create_action:
                self._create_task_for_row(
                    self._context_row_name, self._context_datetime
                )
            elif action == paste_action:
                self.paste_to_row(self._context_row_name, self._context_datetime)

    def _create_task_for_row(self, row_name, when=None):
        seed = {
            "dropoff": row_name,
            "release_datetime": self._format_cell_datetime(when or self.day_start),
        }
        dialog = TaskFormDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            seed=seed,
            default_task_id=self._next_task_id(),
            group_resolver=self._group_for_location,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.items.append(dialog.result)
            self.selected_task_index = len(self.items) - 1
            self.selected_row_name = row_name
            self.refresh_matrix()

    def copy_selected_task(self):
        if self.selected_task_index is None:
            self.status_label.setText("Select a task first")
            return
        self.copied_task = deepcopy(self.items[self.selected_task_index])
        self.status_label.setText(f"Copied {self.copied_task.get('id', '')}")

    def paste_to_selected_row(self):
        if not self.selected_row_name:
            self.status_label.setText("Select a destination row first")
            return
        self.paste_to_row(self.selected_row_name)

    def paste_to_row(self, row_name, when=None):
        if not self._can_create_manual_task():
            return
        if not self.copied_task:
            self.status_label.setText("Copy a task first")
            return
        copied = deepcopy(self.copied_task)
        copied["id"] = self._next_task_id()
        copied["dropoff"] = row_name
        if when is not None:
            copied["release_datetime"] = self._format_cell_datetime(when)
        self.items.append(copied)
        self.selected_task_index = len(self.items) - 1
        self.selected_row_name = row_name
        self.status_label.setText(
            f"Pasted {copied.get('id', '')} to {row_name} at {copied.get('release_datetime', '')}"
        )
        self.refresh_matrix()

    def delete_selected_task(self):
        if self.selected_task_index is None:
            self.status_label.setText("Select a task first")
            return
        task = self.items[self.selected_task_index]
        if (
            QMessageBox.question(
                self, "Delete task", f"Delete task {task.get('id', '')}?"
            )
            != QMessageBox.Yes
        ):
            return
        del self.items[self.selected_task_index]
        self.selected_task_index = None
        self.status_label.setText("Task deleted")
        self.refresh_matrix()

    def _tasks_for_exact_day(self, day_start):
        day_end = day_start + timedelta(days=1)
        results = []
        for idx, task in enumerate(self.items):
            dt = self._task_datetime(task)
            if dt is None:
                continue
            if day_start <= dt < day_end:
                results.append((idx, task, dt))
        return results

    def _shift_task_to_day(
        self, task, source_day_start, target_day_start, reserved_ids
    ):
        copied = deepcopy(task)
        original_dt = self._task_datetime(task)
        if original_dt is None:
            return None
        offset = original_dt - source_day_start
        new_dt = target_day_start + offset
        new_id = self._next_task_id(reserved_ids)
        reserved_ids.add(new_id)
        copied["id"] = new_id
        copied["release_datetime"] = new_dt.replace(second=0, microsecond=0).isoformat(
            timespec="seconds"
        )
        return copied

    def copy_day_tasks_to_other_day(self):
        source_day_start = self.day_start
        source_tasks = self._tasks_for_exact_day(source_day_start)
        if not source_tasks:
            QMessageBox.information(
                self, "Copy day", "There are no tasks on the displayed day to copy."
            )
            return
        picker = MultiDaySelectDialog(
            self, initial_date=source_day_start + timedelta(days=1)
        )
        if picker.exec() != QDialog.Accepted or not picker.result:
            return
        target_day_starts = []
        for text in picker.result:
            try:
                dt = datetime.fromisoformat(text).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if dt != source_day_start:
                    target_day_starts.append(dt)
            except Exception:
                continue
        if not target_day_starts:
            QMessageBox.critical(
                self,
                "Invalid selection",
                "Select at least one target day different from the current day.",
            )
            return
        existing_summary = []
        for target_day_start in target_day_starts:
            existing_target_tasks = self._tasks_for_exact_day(target_day_start)
            if existing_target_tasks:
                existing_summary.append(
                    f"{target_day_start.strftime('%Y-%m-%d')} ({len(existing_target_tasks)} existing)"
                )
        if existing_summary:
            if (
                QMessageBox.question(
                    self,
                    "Some target days already have tasks",
                    "These days already contain tasks:\n\n"
                    + "\n".join(existing_summary)
                    + "\n\nCopy the current day's tasks as additional tasks?",
                )
                != QMessageBox.Yes
            ):
                return
        if (
            QMessageBox.question(
                self,
                "Confirm copy day",
                f"Copy {len(source_tasks)} task(s) from {source_day_start.strftime('%Y-%m-%d')} to {len(target_day_starts)} selected day(s)?",
            )
            != QMessageBox.Yes
        ):
            return
        reserved_ids = {str(task.get("id", "")) for task in self.items}
        created = []
        for target_day_start in target_day_starts:
            for _idx, task, _dt in source_tasks:
                copied = self._shift_task_to_day(
                    task, source_day_start, target_day_start, reserved_ids
                )
                if copied is not None:
                    created.append(copied)
        self.items.extend(created)
        self.status_label.setText(
            f"Copied {len(source_tasks)} task(s) to {len(target_day_starts)} day(s)"
        )
        self.refresh_matrix()


class EditMultipleTasksDialog(QDialog):
    def __init__(self, parent, profile_names, seed=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Multiple Tasks")
        self.profile_names = list(profile_names)
        self.seed = seed or {}
        self.result = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.release_check = QCheckBox("Update release date and time")
        self.release_edit = _date_time_input(self.seed.get("release_datetime", ""))
        self.release_edit.setEnabled(False)
        self.release_check.toggled.connect(self.release_edit.setEnabled)
        release_row = QHBoxLayout()
        release_row.addWidget(self.release_check)
        release_row.addWidget(self.release_edit, 1)
        form.addRow("Release date and time", release_row)

        self.target_check = QCheckBox("Update target allowance")
        self.target_edit = _integer_input(self.seed.get("target_time", 0), minimum=0, maximum=31_536_000, suffix=" s")
        self.target_edit.setEnabled(False)
        self.target_check.toggled.connect(self.target_edit.setEnabled)
        target_row = QHBoxLayout()
        target_row.addWidget(self.target_check)
        target_row.addWidget(self.target_edit, 1)
        form.addRow("Target allowance", target_row)

        self.priority_check = QCheckBox("Update priority")
        self.priority_edit = _integer_input(self.seed.get("priority", 0), minimum=0, maximum=999_999)
        self.priority_edit.setEnabled(False)
        self.priority_check.toggled.connect(self.priority_edit.setEnabled)
        priority_row = QHBoxLayout()
        priority_row.addWidget(self.priority_check)
        priority_row.addWidget(self.priority_edit, 1)
        form.addRow("Priority", priority_row)

        self.labels_check = QCheckBox("Update labels")
        self.labels_edit = QLineEdit(", ".join(self.seed.get("labels", [])))
        self.labels_edit.setEnabled(False)
        self.labels_check.toggled.connect(self.labels_edit.setEnabled)
        labels_row = QHBoxLayout()
        labels_row.addWidget(self.labels_check)
        labels_row.addWidget(self.labels_edit, 1)
        form.addRow("Labels (comma-separated)", labels_row)

        self.route_profile_check = QCheckBox("Update route profile")
        self.route_profile_combo = QComboBox()
        self.route_profile_combo.addItems(self.profile_names)
        self.route_profile_combo.setCurrentText(str(self.seed.get("route_profile", "")))
        self.route_profile_combo.setEnabled(False)
        self.route_profile_check.toggled.connect(self.route_profile_combo.setEnabled)
        route_profile_row = QHBoxLayout()
        route_profile_row.addWidget(self.route_profile_check)
        route_profile_row.addWidget(self.route_profile_combo, 1)
        form.addRow("Route profile", route_profile_row)

        self.delivery_resource_check = QCheckBox("Update delivery resource")
        self.delivery_resource_combo = _delivery_resource_combo(
            self.seed.get("delivery_resource", self.seed.get("delivery_method", "amr"))
        )
        self.delivery_resource_combo.setEnabled(False)
        self.delivery_resource_check.toggled.connect(self.delivery_resource_combo.setEnabled)
        delivery_resource_row = QHBoxLayout()
        delivery_resource_row.addWidget(self.delivery_resource_check)
        delivery_resource_row.addWidget(self.delivery_resource_combo, 1)
        form.addRow("Delivery resource", delivery_resource_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 260)
        _polish_dialog(self)

    def accept(self):
        try:
            updates = {}
            if self.release_check.isChecked():
                updates["release_datetime"] = self.release_edit.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss")
            if self.target_check.isChecked():
                updates["target_time"] = int(self.target_edit.value())
            if self.priority_check.isChecked():
                updates["priority"] = int(self.priority_edit.value())
            if self.labels_check.isChecked():
                text = self.labels_edit.text().strip()
                updates["labels"] = [x.strip() for x in text.split(",")] if text else []
            if self.route_profile_check.isChecked():
                updates["route_profile"] = (
                    self.route_profile_combo.currentText().strip()
                )
            if self.delivery_resource_check.isChecked():
                updates["delivery_resource"] = normalise_delivery_resource_policy(
                    {"mode": self.delivery_resource_combo.currentData()}
                )
            if not updates:
                raise ValueError("Select at least one field to update")
            self.result = updates
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid task updates", str(exc))


class TaskEditorWindow(QMainWindow):
    columns = [
        ("id", "ID", 90),
        ("pickup", "Pickup", 150),
        ("dropoff", "Dropoff", 150),
        ("payload", "Payload", 130),
        ("release_datetime", "Release datetime", 170),
        ("target_time", "Target time", 90),
        ("priority", "Priority", 80),
        ("labels", "Labels", 150),
        ("route_profile", "Route profile", 120),
        ("delivery_resource", "Delivery resource", 125),
    ]

    def __init__(
        self,
        master,
        items,
        location_names,
        payload_names,
        profile_names,
        suggest_task_id,
        on_save,
        floor_map=None,
        manual_single_payload_available=True,
    ):
        super().__init__(master)
        self.setWindowTitle("Tasks")
        self.resize(1200, 520)
        self.items = items
        self.location_names = location_names
        self.payload_names = payload_names
        self.profile_names = profile_names
        self.suggest_task_id = suggest_task_id
        self.on_save = on_save
        self.floor_map = floor_map or {}
        self.manual_single_payload_available = bool(manual_single_payload_available)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels([c[1] for c in self.columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._on_tree_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for idx, (_key, _heading, width) in enumerate(self.columns):
            self.table.setColumnWidth(idx, width)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        for text, handler in [
            ("Add", self.add_item),
            ("Edit", self.edit_item),
            ("Delete", self.delete_item),
            ("Duplicate x Times", self._duplicate_selected_item),
            ("One to Many", self._create_one_to_many_tasks),
            ("Schedule Return Trip", self._schedule_return_trips),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save)
        buttons.addWidget(save_btn)

        self._refresh_table()
        self.show()
        _polish_dialog(self)

    def _insert_tree_item(self, item):
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            item.get("id", ""),
            item.get("pickup", ""),
            item.get("dropoff", ""),
            item.get("payload", ""),
            item.get("release_datetime", ""),
            item.get("target_time", ""),
            item.get("priority", ""),
            ", ".join(item.get("labels", [])),
            item.get("route_profile", ""),
            normalise_delivery_resource_policy(
                item.get("delivery_resource", item.get("delivery_method", "amr"))
            ).get("mode", "amr").title(),
        ]
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def _refresh_table(self):
        self.table.setRowCount(0)
        for item in self.items:
            self._insert_tree_item(item)

    def _selected_row_indexes(self):
        return sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})

    def _next_task_id(self, reserved_ids=None):
        reserved_ids = set(reserved_ids or [])
        nums = []
        for task in self.items:
            task_id = str(task.get("id", ""))
            if task_id.startswith("T") and task_id[1:].isdigit():
                nums.append(int(task_id[1:]))
        for task_id in reserved_ids:
            task_id = str(task_id)
            if task_id.startswith("T") and task_id[1:].isdigit():
                nums.append(int(task_id[1:]))
        next_num = max(nums, default=0) + 1
        return f"T{next_num}"

    def _group_for_location(self, item):
        floor = getattr(self, "floor_map", {}).get(item)
        return f"Floor {floor}" if floor is not None else "Other"

    def _can_create_manual_task(self):
        if self.manual_single_payload_available:
            return True
        QMessageBox.critical(
            self,
            "Manual task unavailable",
            "Manual task creation requires a staff delivery type or an AMR with exactly one payload slot. Multi-stop AMRs are reserved for simulator route batching.",
        )
        return False

    def add_item(self):
        if not self._can_create_manual_task():
            return
        dialog = TaskFormDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            default_task_id=self._next_task_id(),
            group_resolver=self._group_for_location,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.items.append(dialog.result)
            self._insert_tree_item(dialog.result)

    def edit_multiple_items(self):
        rows = self._selected_row_indexes()
        if len(rows) < 2:
            QMessageBox.critical(
                self, "Not enough tasks selected", "Select two or more tasks first."
            )
            return

        first_task = self.items[rows[0]]
        dialog = EditMultipleTasksDialog(
            self,
            self.profile_names,
            seed={
                "release_datetime": first_task.get("release_datetime", ""),
                "target_time": first_task.get("target_time", ""),
                "priority": first_task.get("priority", ""),
                "labels": list(first_task.get("labels", [])),
                "route_profile": first_task.get("route_profile", ""),
            },
        )
        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return

        updates = dialog.result
        for idx in rows:
            updated = deepcopy(self.items[idx])
            updated.update(deepcopy(updates))
            self.items[idx] = updated

        self._refresh_table()
        for idx in rows:
            self.table.selectRow(idx)

    def edit_item(self):
        rows = self._selected_row_indexes()
        if not rows:
            return
        idx = rows[0]
        dialog = TaskFormDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            seed=self.items[idx],
            default_task_id=self.items[idx].get("id", self._next_task_id()),
            group_resolver=self._group_for_location,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.items[idx] = dialog.result
            self._refresh_table()
            self.table.selectRow(idx)

    def _show_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return

        selected_rows = self._selected_row_indexes()
        if row not in selected_rows:
            self.table.clearSelection()
            self.table.selectRow(row)
            selected_rows = [row]

        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        edit_multiple_action = None
        if len(selected_rows) > 1:
            edit_multiple_action = menu.addAction("Edit Multiple")
        delete_action = menu.addAction("Delete")

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == edit_action:
            self.edit_item()
        elif edit_multiple_action is not None and action == edit_multiple_action:
            self.edit_multiple_items()
        elif action == delete_action:
            self.delete_item()

    def _parse_return_delay(self, text):
        value = (text or "").strip()
        if not value:
            raise ValueError("Delay is required")
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError("Delay must be in HH:MM:SS format")
        hours, minutes, seconds = [int(x) for x in parts]
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)

    def delete_item(self):
        rows = self._selected_row_indexes()
        if not rows:
            return
        for idx in reversed(rows):
            del self.items[idx]
        self._refresh_table()

    def save(self):
        self.on_save(self.items)
        self.close()

    def _duplicate_selected_item(self):
        rows = self._selected_row_indexes()
        if not rows:
            QMessageBox.critical(
                self, "No task selected", "Select a task to duplicate."
            )
            return
        idx = rows[0]
        source_task = self.items[idx]
        count = ask_int(
            self,
            "Duplicate task",
            "How many copies do you want to create?",
            value=1,
            minimum=1,
        )
        if count is None:
            return
        insert_at = idx + 1
        new_items = []
        reserved_ids = {str(task.get("id", "")) for task in self.items}
        for _ in range(count):
            copied = deepcopy(source_task)
            new_id = self._next_task_id(reserved_ids)
            copied["id"] = new_id
            reserved_ids.add(new_id)
            new_items.append(copied)
        for offset, copied in enumerate(new_items):
            self.items.insert(insert_at + offset, copied)
        self._refresh_table()

    def _on_tree_double_click(self, row, _col):
        self.table.selectRow(row)
        self.edit_item()

    def _create_one_to_many_tasks(self):
        if not self._can_create_manual_task():
            return
        dialog = BulkOneToManyTaskDialog(
            self,
            self.location_names,
            self.payload_names,
            self.profile_names,
            group_resolver=self._group_for_location,
            default_task_id=self._next_task_id(),
        )
        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return
        payload = dialog.result
        reserved_ids = {str(task.get("id", "")) for task in self.items}
        created = []
        for dropoff in payload["dropoffs"]:
            new_id = self._next_task_id(reserved_ids)
            reserved_ids.add(new_id)
            created.append(
                {
                    "id": new_id,
                    "pickup": payload["pickup"],
                    "dropoff": dropoff,
                    "payload": payload["payload"],
                    "release_datetime": payload["release_datetime"],
                    "target_time": payload["target_time"],
                    "priority": payload["priority"],
                    "labels": list(payload["labels"]),
                    "route_profile": payload["route_profile"],
                    "delivery_resource": deepcopy(payload["delivery_resource"]),
                    "manual_task": True,
                    "manual_single_payload_only": True,
                }
            )
        self.items.extend(created)
        self._refresh_table()

    def _apply_delay_to_release_datetime(self, release_datetime_text, delay):
        base_dt = datetime.fromisoformat(release_datetime_text)
        return (base_dt + delay).isoformat(timespec="seconds")

    def _schedule_return_trips(self):
        rows = self._selected_row_indexes()
        if not rows:
            QMessageBox.critical(
                self, "No task selected", "Select one or more tasks first."
            )
            return
        delay_text = ask_string(
            self, "Return trip delay", "Enter delay as HH:MM:SS", text="00:30:00"
        )
        if delay_text is None:
            return
        try:
            delay = self._parse_return_delay(delay_text)
        except Exception as exc:
            QMessageBox.critical(self, "Invalid delay", str(exc))
            return
        reserved_ids = {str(task.get("id", "")) for task in self.items}
        new_tasks = []
        for idx in rows:
            source_task = self.items[idx]
            copied = deepcopy(source_task)
            new_id = self._next_task_id(reserved_ids)
            reserved_ids.add(new_id)
            copied["id"] = new_id
            copied["pickup"] = source_task.get("dropoff", "")
            copied["dropoff"] = source_task.get("pickup", "")
            copied["release_datetime"] = self._apply_delay_to_release_datetime(
                source_task.get("release_datetime", "2026-01-01T08:00:00"), delay
            )
            new_tasks.append(copied)
        self.items.extend(new_tasks)
        self._refresh_table()
