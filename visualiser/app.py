"""
AMR Simulator app
"""

import json
import math
import sys
from pathlib import Path

import os
from concurrent.futures import ProcessPoolExecutor, as_completed

from PySide6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    Qt,
    Signal,
    QRect,
    QThread,
    Slot,
    QRectF,
)
from PySide6.QtGui import QAction, QColor, QBrush, QPainter, QPen, QFont, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGraphicsItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
    QMenu,
)

from dxf_scene import DXFScene
from dialogs import (
    EdgeConnectionsDialog,
    LiftEditorDialog,
    LiftListDialog,
    PointEditorDialog,
    TableListEditor,
    DepartmentEditorDialog,
    WasteStreamEditorDialog,
    WasteStreamListDialog,
    MassCollectionListDialog,
    DepartmentListDialog,
    AMRListDialog,
    AMREditorDialog,
    InventorySpacesDialog,
    TaskGenerationSettingsDialog,
    PayloadListDialog,
    SimulationSettingsDialog,
    PeopleMovementListDialog,
    ScenarioTestingDialog,
    CorridorSettingsDialog,
)
from advanced_dialogs import (
    MultiSelectPicker,
    RouteProfilesEditorV2,
    TaskEditorWindow,
    TaskPlannerDialog,
)
from models import JsonStore
from ui_theme import install_application_theme, polish_dialog as _polish_dialog


def _load_dxf_floor_process(args):
    floor, path = args
    try:
        payload = DXFScene.load_content(path)
        return {
            "ok": True,
            "floor": int(floor),
            "path": str(path),
            "entities": payload["entities"],
            "bounds": payload["bounds"],
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "floor": int(floor),
            "path": str(path),
            "entities": None,
            "bounds": None,
            "error": str(exc),
        }


class DXFLoadWorker(QObject):
    loaded = Signal(int, str, object, object)
    failed = Signal(int, str, str)
    finished_batch = Signal()

    @Slot(object)
    def load_floors(self, jobs):
        jobs = list(jobs or [])
        if not jobs:
            self.finished_batch.emit()
            return

        worker_count = min(len(jobs), max(1, (os.cpu_count() or 2) - 1))

        try:
            with ProcessPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_load_dxf_floor_process, job) for job in jobs]

                for future in as_completed(futures):
                    result = future.result()
                    floor = int(result["floor"])
                    path = str(result["path"])

                    if result.get("ok"):
                        self.loaded.emit(
                            floor,
                            path,
                            result["entities"],
                            result["bounds"],
                        )
                    else:
                        self.failed.emit(
                            floor,
                            path,
                            str(result.get("error", "Unknown DXF load error")),
                        )
        finally:
            self.finished_batch.emit()


class DXFLoadingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._completed = False
        self.setWindowTitle("Loading DXFs")
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        self.message_label = QLabel("Loading DXF files...")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("0 / 0")
        layout.addWidget(self.detail_label)
        _polish_dialog(self)

    def update_progress(self, current, total, message, failed_count=0):
        total = max(1, int(total))
        current = max(0, min(int(current), total))
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.message_label.setText(message)
        detail = f"{current} / {total} loaded"
        if failed_count:
            detail += f" ({failed_count} failed)"
        self.detail_label.setText(detail)

    def mark_complete(self):
        self._completed = True
        self.accept()

    def reject(self):
        if self._completed:
            super().reject()

    def closeEvent(self, event):
        if self._completed:
            super().closeEvent(event)
        else:
            event.ignore()


class EditorGraphicsView(QGraphicsView):
    leftClicked = Signal(object, float, float)
    leftDoubleClicked = Signal(object, float, float)
    leftReleased = Signal(object)
    rightClicked = Signal(object, float, float)
    middleClicked = Signal(object)
    middleDragged = Signal(object)
    middleReleased = Signal(object)
    mouseWheelScrolled = Signal(object)
    mouseDragged = Signal(object, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing, False)
        self.setBackgroundBrush(QBrush(QColor("#111111")))
        self._overlay_provider = None
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self._middle_panning = False
        self._last_middle_pos = None

    def mousePressEvent(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.LeftButton:
            self.leftClicked.emit(event, scene_pos.x(), scene_pos.y())
        elif event.button() == Qt.RightButton:
            self.rightClicked.emit(event, scene_pos.x(), scene_pos.y())
        elif event.button() == Qt.MiddleButton:
            self._middle_panning = True
            self._last_middle_pos = event.position().toPoint()
            self.middleClicked.emit(event)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.LeftButton:
            self.leftDoubleClicked.emit(event, scene_pos.x(), scene_pos.y())
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.leftReleased.emit(event)
        elif event.button() == Qt.MiddleButton:
            self._middle_panning = False
            self._last_middle_pos = None
            self.middleReleased.emit(event)
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())
        if self._middle_panning and self._last_middle_pos is not None:
            self.middleDragged.emit(event)
        if event.buttons() & Qt.LeftButton:
            self.mouseDragged.emit(event, scene_pos.x(), scene_pos.y())
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self.mouseWheelScrolled.emit(event)
        event.accept()

    def set_overlay_provider(self, overlay_provider):
        self._overlay_provider = overlay_provider
        self.viewport().update()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self._overlay_provider:
            painter.save()
            painter.resetTransform()
            self._overlay_provider(painter, self.viewport().rect())
            painter.restore()


class AMRGraphEditor(QMainWindow):
    _request_dxf_batch_load = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AMR Simulation Graph Editor")
        self.resize(1500, 920)

        self.store = JsonStore()
        self.current_json_path = None
        self.current_dxf_path = None
        self.loaded_dxf_floor = None
        self.dxf_scene = DXFScene()
        self._dxf_cache = {}
        self._dxf_loading_floors = set()
        self._pending_fit_after_load = False
        self._last_requested_floor = None
        self._loading_dialog = None
        self._loading_batch_floors = set()
        self._loading_batch_failed = set()
        self._loading_batch_active = False

        self.route_profile_selection_active = False
        self.route_profile_allowed_point_names = set()
        self.route_profile_selected_nodes = set()
        self.route_profile_selection_callback = None
        self.route_profile_return_window = None
        self.route_profile_selection_rect_start = None
        self.route_profile_selection_rect_item = None

        self.department_location_placement_active = False
        self.department_location_placement_name = None
        self.department_location_placement_category_key = None
        self.department_location_placement_callback = None
        self.department_location_placement_return_dialog = None

        self._dxf_thread = QThread(self)
        self._dxf_worker = DXFLoadWorker()
        self._dxf_worker.moveToThread(self._dxf_thread)
        self._dxf_worker.loaded.connect(self._on_dxf_loaded)
        self._dxf_worker.failed.connect(self._on_dxf_failed)
        self._request_dxf_batch_load.connect(self._dxf_worker.load_floors)
        self._dxf_worker.finished_batch.connect(self._update_loading_dialog)
        self._dxf_thread.start()

        self.scale = 5.0
        self.offset_x = 250
        self.offset_y = 250
        self.last_pan = None
        self.selected_for_edge = None
        self.selected_point_name = None
        self.selected_point_names = set()
        self.selected_edge_keys = set()
        self.topology_selection_rect_start = None
        self.topology_selection_rect_item = None
        self.dragging_point_name = None
        self.drag_mode_active = False
        self.edge_delete_start = None
        self.bounding_box_location_name = None
        self.bounding_box_points = []
        self.dragging_bounding_box_point_index = None

        self._item_lookup = {}
        self._point_item_lookup = {}

        self._build_ui()
        self.refresh_canvas()

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scene = QGraphicsScene(self)
        self.canvas = EditorGraphicsView(self)
        self.canvas.setScene(self.scene)
        self.canvas.set_overlay_provider(self.draw_overlay_panels)

        self.canvas.leftClicked.connect(self.on_left_click)
        self.canvas.leftDoubleClicked.connect(self.on_double_click)
        self.canvas.leftReleased.connect(self.on_left_release)
        self.canvas.rightClicked.connect(self.on_right_click)
        self.canvas.middleClicked.connect(self.on_middle_click)
        self.canvas.middleDragged.connect(self.on_middle_drag)
        self.canvas.middleReleased.connect(self.on_middle_release)
        self.canvas.mouseWheelScrolled.connect(self.on_mousewheel)
        self.canvas.mouseDragged.connect(self.on_drag)

        self.mode_combo = QComboBox()
        edit_modes = [
            ("Select and move", "select_move", "Select, multi-select, drag and edit existing items."),
            ("Add corridor node", "corridor_node", "Place a graph node used by corridor routes and door openings."),
            ("Add location", "location", "Place a general simulation location."),
            ("Draw location boundary", "location_bbox", "Draw or replace the usable boundary around a location."),
            ("Add department location", "department", "Place a location associated with a department workflow."),
            ("Connect corridor", "edge", "Create a corridor edge between graph nodes."),
            ("Add lift", "lift", "Place or edit a lift access point."),
            ("Pan view", "pan", "Move around the drawing without selecting assets."),
            ("Delete items", "delete", "Delete the item clicked in the drawing."),
        ]
        for label, value, tooltip in edit_modes:
            self.mode_combo.addItem(label, value)
            self.mode_combo.setItemData(self.mode_combo.count() - 1, tooltip, Qt.ToolTipRole)
        self.mode_combo.setToolTip("Choose what a mouse click does in the topology editor.")
        self.floor_spin = QSpinBox()
        self.floor_spin.setRange(0, 99)
        self.floor_spin.valueChanged.connect(self.on_floor_changed)
        self.snap_check = QCheckBox("Snap to 1.0")
        self.snap_check.setChecked(True)
        self.chain_edges_check = QCheckBox("Chain edge creation")
        self.chain_edges_check.setChecked(True)
        self.bidirectional_check = QCheckBox("Bidirectional edges")
        self.bidirectional_check.setChecked(True)
        self.show_dxf_check = QCheckBox("Show DXF")
        self.show_dxf_check.setChecked(True)
        self.show_labels_check = QCheckBox("Show labels")
        self.show_labels_check.setChecked(True)
        self.show_location_bounds_check = QCheckBox("Show location bounding boxes")
        self.show_location_bounds_check.setChecked(False)
        self.show_charging_spaces_check = QCheckBox("Show charging spaces")
        self.show_charging_spaces_check.setChecked(True)
        self.show_dxf_check.toggled.connect(self.refresh_canvas)
        self.show_labels_check.toggled.connect(self.refresh_canvas)
        self.show_location_bounds_check.toggled.connect(self.refresh_canvas)
        self.show_charging_spaces_check.toggled.connect(self.refresh_canvas)

        self.ribbon_tabs = QTabWidget()
        self.ribbon_tabs.setDocumentMode(True)
        self.ribbon_tabs.tabBar().setUsesScrollButtons(True)
        self.ribbon_tabs.setElideMode(Qt.ElideRight)
        self.ribbon_tabs.setMinimumHeight(160)
        self.ribbon_tabs.setMaximumHeight(240)
        layout.addWidget(self.ribbon_tabs)

        ribbon_pages = []

        def create_page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(False)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            content.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            page_layout = QHBoxLayout(content)
            page_layout.setContentsMargins(6, 5, 6, 5)
            page_layout.setSpacing(6)
            scroll.setWidget(content)
            self.ribbon_tabs.addTab(scroll, title)
            ribbon_pages.append((content, page_layout))
            return page_layout

        def add_group(page_layout, title, widgets, columns=2):
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setContentsMargins(7, 6, 7, 6)
            grid.setHorizontalSpacing(5)
            grid.setVerticalSpacing(4)
            row = 0
            col = 0
            for widget in widgets:
                if isinstance(widget, tuple):
                    label, control = widget
                    grid.addWidget(QLabel(label), row, col)
                    grid.addWidget(control, row, col + 1)
                    row += 1
                    col = 0
                    continue
                grid.addWidget(widget, row, col)
                col += 1
                if col >= columns:
                    row += 1
                    col = 0
            page_layout.addWidget(box)
            return box

        def button(text, handler, tooltip=""):
            control = QPushButton(text)
            control.clicked.connect(handler)
            if tooltip:
                control.setToolTip(tooltip)
            return control

        home = create_page("Home")
        add_group(
            home,
            "File",
            [
                button("Open JSON", self.open_json),
                button("Save JSON", self.save_json),
                button("Validate", self.validate_json),
            ],
            columns=3,
        )
        add_group(
            home,
            "Drawing",
            [
                button("Map DXF to floor", self.load_dxf),
                button("Clear floor DXF", self.clear_floor_dxf),
                button("Fit view", self.fit_view),
            ],
            columns=3,
        )

        edit = create_page("Edit")
        add_group(edit, "Placement", [("Mode", self.mode_combo)], columns=2)
        floor_go = button("Go", self.refresh_canvas)
        floor_widget = QWidget()
        floor_layout = QHBoxLayout(floor_widget)
        floor_layout.setContentsMargins(0, 0, 0, 0)
        floor_layout.addWidget(self.floor_spin)
        floor_layout.addWidget(floor_go)
        add_group(edit, "Floor", [("Current floor", floor_widget)], columns=2)
        add_group(
            edit,
            "Drawing behaviour",
            [self.snap_check, self.bidirectional_check, self.chain_edges_check],
            columns=1,
        )

        view = create_page("View")
        add_group(
            view,
            "Display",
            [
                self.show_dxf_check,
                self.show_labels_check,
                self.show_location_bounds_check,
                self.show_charging_spaces_check,
            ],
            columns=2,
        )
        add_group(view, "Navigation", [button("Fit view", self.fit_view)], columns=1)

        simulation = create_page("Simulation")
        add_group(
            simulation,
            "Configuration",
            [
                button("Simulation settings", self.manage_simulation_settings),
                button("Scenario testing", self.manage_scenario_testing),
                button("People movement", self.manage_people_movement),
                button("Corridor widths and doors", self.manage_corridor_settings),
            ],
            columns=2,
        )
        classification_button = QPushButton("Classify selected corridors")
        classification_menu = QMenu(classification_button)
        for label, value in [
            ("No restriction", "none"),
            ("Staff", "staff"),
            ("Public", "public"),
            ("Mixed staff/public", "both"),
        ]:
            action = classification_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, area=value: self.classify_selected_corridors(area)
            )
        classification_button.setMenu(classification_menu)
        door_button = QPushButton("Set selected door nodes")
        door_menu = QMenu(door_button)
        mark_door = door_menu.addAction("Mark as door…")
        clear_door = door_menu.addAction("Remove door classification")
        mark_door.triggered.connect(self.mark_selected_nodes_as_doors)
        clear_door.triggered.connect(self.clear_selected_node_doors)
        door_button.setMenu(door_menu)
        add_group(
            simulation,
            "Topology selection",
            [
                button("Scenario from selection", self.manage_scenario_testing),
                button("People profile from selection", self.create_people_profile_from_selection),
                button("Edit selected corridors", self.manage_corridor_settings),
                classification_button,
                door_button,
                button("Clear selection", self.clear_topology_selection),
            ],
            columns=2,
        )

        assets = create_page("Assets")
        add_group(
            assets,
            "AMR system",
            [
                button("AMRs", self.manage_amrs),
                button("Payloads", self.manage_payloads),
                button("Charging locations", self.manage_charging_locations),
                button("Lifts", self.manage_lifts),
            ],
            columns=2,
        )

        operations = create_page("Operations")
        add_group(
            operations,
            "Tasks and routing",
            [
                button("Tasks", self.manage_tasks),
                button("Task planner", self.manage_task_planner),
                button("Task generation", self.manage_task_generation),
                button("Route profiles", self.manage_route_profiles),
            ],
            columns=2,
        )

        services = create_page("Services")
        add_group(
            services,
            "Departments and waste",
            [
                button("Departments", self.manage_departments),
                button("Waste streams", self.manage_waste_streams),
                button("Mass collections", self.manage_mass_collections),
            ],
            columns=3,
        )

        for content, page_layout in ribbon_pages:
            page_layout.addStretch(1)
            content.adjustSize()

        layout.addWidget(self.canvas, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setMinimumWidth(240)
        self.file_label = QLabel("New file")
        self.file_label.setToolTip("Current JSON file")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.file_label)

    def _current_edit_mode(self):
        return str(self.mode_combo.currentData() or "select_move")

    def _set_edit_mode(self, value):
        index = self.mode_combo.findData(str(value))
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def set_status(self, text):
        self.status_label.setText(text)

    @staticmethod
    def _physical_edge_key(from_name, to_name):
        a = str(from_name or "").strip()
        b = str(to_name or "").strip()
        return tuple(sorted((a, b)))

    def _edge_label_for_key(self, key):
        for edge in self.store.data.get("corridors", {}).get("edges", []):
            if self._physical_edge_key(edge.get("from"), edge.get("to")) == key:
                return f"{edge.get('from', '')} -> {edge.get('to', '')}"
        return f"{key[0]} -> {key[1]}"

    def _selected_corridor_keys(self, include_incident_nodes=True):
        keys = set(self.selected_edge_keys)
        if include_incident_nodes:
            node_names = set(self.selected_point_names)
            for edge in self.store.data.get("corridors", {}).get("edges", []):
                if edge.get("from") in node_names or edge.get("to") in node_names:
                    keys.add(self._physical_edge_key(edge.get("from"), edge.get("to")))
        return keys

    def selected_corridor_labels(self, include_incident_nodes=True):
        return sorted(
            self._edge_label_for_key(key)
            for key in self._selected_corridor_keys(include_incident_nodes)
        )

    def selected_corridor_node_names(self):
        points = self.store.all_points()
        return sorted(
            name
            for name in self.selected_point_names
            if points.get(name, {}).get("kind") == "corridor_node"
        )

    def topology_selection_payload(self):
        return {
            "corridor": self.selected_corridor_labels(include_incident_nodes=False),
            "corridor_node": self.selected_corridor_node_names(),
        }

    def clear_topology_selection(self):
        self.selected_point_names.clear()
        self.selected_edge_keys.clear()
        self.selected_point_name = None
        self.dragging_point_name = None
        self.drag_mode_active = False
        self.set_status("Topology selection cleared")
        self.refresh_canvas()

    def find_nearest_edge_key(self, x, y, floor, radius_world=0.45):
        points = self.store.all_points()
        best_key = None
        best_distance = float(radius_world)
        visited = set()
        for edge in self.store.data.get("corridors", {}).get("edges", []):
            a = points.get(edge.get("from"))
            b = points.get(edge.get("to"))
            if not a or not b:
                continue
            if int(a.get("floor", -1)) != int(floor) or int(b.get("floor", -1)) != int(floor):
                continue
            key = self._physical_edge_key(edge.get("from"), edge.get("to"))
            if key in visited:
                continue
            visited.add(key)
            ax, ay = float(a.get("x", 0.0)), float(a.get("y", 0.0))
            bx, by = float(b.get("x", 0.0)), float(b.get("y", 0.0))
            dx, dy = bx - ax, by - ay
            length_sq = dx * dx + dy * dy
            if length_sq <= 1e-12:
                distance = math.hypot(x - ax, y - ay)
            else:
                t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length_sq))
                px, py = ax + t * dx, ay + t * dy
                distance = math.hypot(x - px, y - py)
            if distance <= best_distance:
                best_distance = distance
                best_key = key
        return best_key

    def classify_selected_corridors(self, area_type):
        keys = self._selected_corridor_keys(include_incident_nodes=True)
        if not keys:
            QMessageBox.information(
                self,
                "Corridor classification",
                "Select one or more corridor edges or corridor nodes first.",
            )
            return
        area = str(area_type or "none").lower()
        if area == "mixed":
            area = "both"
        updated = 0
        for edge in self.store.data.get("corridors", {}).get("edges", []):
            if self._physical_edge_key(edge.get("from"), edge.get("to")) in keys:
                edge["people_area_type"] = area
                updated += 1
        self.set_status(f"Classified {len(keys)} corridor asset(s) as {area}")
        self.refresh_canvas()

    def mark_selected_nodes_as_doors(self):
        names = self.selected_corridor_node_names()
        if not names:
            QMessageBox.information(
                self, "Door nodes", "Select one or more corridor nodes first."
            )
            return
        default_width = float(
            self.store.data.get("building", {}).get("default_door_clear_width_m", 0.9)
            or 0.9
        )
        width, ok = QInputDialog.getDouble(
            self,
            "Door clear opening",
            "Clear opening width (m):",
            default_width,
            0.1,
            20.0,
            3,
        )
        if not ok:
            return
        for node in self.store.data.get("corridors", {}).get("nodes", []):
            if node.get("name") in names:
                node["has_door"] = True
                node["door_clear_width_m"] = float(width)
        self.set_status(f"Marked {len(names)} corridor node(s) as {width:.3f} m doors")
        self.refresh_canvas()

    def clear_selected_node_doors(self):
        names = self.selected_corridor_node_names()
        if not names:
            QMessageBox.information(
                self, "Door nodes", "Select one or more corridor nodes first."
            )
            return
        for node in self.store.data.get("corridors", {}).get("nodes", []):
            if node.get("name") in names:
                node["has_door"] = False
        self.set_status(f"Removed door classification from {len(names)} node(s)")
        self.refresh_canvas()

    def create_people_profile_from_selection(self):
        selected = self.selected_corridor_labels(include_incident_nodes=True)
        if not selected:
            QMessageBox.information(
                self,
                "People movement",
                "Select one or more corridor edges or corridor nodes first.",
            )
            return
        locations = sorted(self.store.all_points().keys())
        corridor_options = sorted(
            {
                f"{edge.get('from', '')} -> {edge.get('to', '')}"
                for edge in self.store.data.get("corridors", {}).get("edges", [])
                if edge.get("from") and edge.get("to")
            }
        )
        seed = {"id": f"PEOPLE-{len(self.store.people_movements()) + 1}"}
        from dialogs import PeopleMovementEditorDialog

        dialog = PeopleMovementEditorDialog(
            self,
            locations,
            corridor_options,
            seed=seed,
            initially_selected_corridors=selected,
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            movements = list(self.store.people_movements())
            movements.append(dialog.result)
            self.store.set_people_movements(movements)
            profile_id = dialog.result.get("id", "")
            selected_keys = self._selected_corridor_keys(include_incident_nodes=True)
            for edge in self.store.data.get("corridors", {}).get("edges", []):
                if self._physical_edge_key(edge.get("from"), edge.get("to")) in selected_keys:
                    profile_ids = list(edge.get("people_profile_ids", []) or [])
                    if profile_id and profile_id not in profile_ids:
                        profile_ids.append(profile_id)
                    edge["people_profile_ids"] = profile_ids
                    group = str(dialog.result.get("group_type", "staff") or "staff")
                    edge["people_area_type"] = "both" if group == "both" else group
            self.set_status(
                f"Created people profile {profile_id} for {len(selected_keys)} corridor asset(s)"
            )
            self.refresh_canvas()

    def start_department_location_placement(
        self,
        location_name,
        category_key,
        callback,
        return_dialog=None,
    ):
        self.department_location_placement_active = True
        self.department_location_placement_name = str(location_name).strip()
        self.department_location_placement_category_key = str(category_key).strip()
        self.department_location_placement_callback = callback
        self.department_location_placement_return_dialog = return_dialog

        self._set_edit_mode("select_move")
        self.set_status(
            f"Click the DXF/editor scene to place location {self.department_location_placement_name}"
        )

    def cancel_department_location_placement(self):
        self.department_location_placement_active = False
        self.department_location_placement_name = None
        self.department_location_placement_category_key = None
        self.department_location_placement_callback = None

        dialog = self.department_location_placement_return_dialog
        self.department_location_placement_return_dialog = None

        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        self.set_status("Department location placement cancelled")

    def finish_department_location_placement(self, x, y, floor):
        if not self.department_location_placement_active:
            return False

        location_name = self.department_location_placement_name
        category_key = self.department_location_placement_category_key
        callback = self.department_location_placement_callback
        dialog = self.department_location_placement_return_dialog

        self.department_location_placement_active = False
        self.department_location_placement_name = None
        self.department_location_placement_category_key = None
        self.department_location_placement_callback = None
        self.department_location_placement_return_dialog = None

        payload = {
            "category": category_key,
            "name": location_name,
            "floor": int(floor),
            "x": round(float(x), 3),
            "y": round(float(y), 3),
        }

        if location_name in self.store.names_in_use():
            QMessageBox.critical(
                self,
                "Duplicate location",
                f"A point or location named '{location_name}' already exists.",
            )
            if dialog is not None:
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            return True

        self.store.add_location(
            location_name,
            int(floor),
            round(float(x), 3),
            round(float(y), 3),
        )

        if callable(callback):
            callback(category_key, payload)

        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        self.set_status(f"Created location {location_name}")
        self.refresh_canvas()
        return True

    def task_generation_category_pairs(self):
        task_generation = (
            self.store.task_generation()
            if hasattr(self.store, "task_generation")
            else self.store.data.setdefault("task_generation", {})
        )

        pairs = []
        for key, item in task_generation.get("categories", {}).items():
            if not isinstance(item, dict):
                continue
            label = str(item.get("display_name", "")).strip() or str(key).title()
            suffix = str(
                item.get("department_location_suffix", f"-{key.upper()}")
            ).strip()
            pairs.append((str(key), label, suffix))

        return sorted(pairs, key=lambda x: x[1].lower())

    def create_department_generated_locations(self, department_result):
        created = 0

        for item in department_result.get("_create_locations", []):
            name = str(item.get("name", "")).strip()
            if not name or name in self.store.names_in_use():
                continue

            self.store.add_location(
                name,
                int(item.get("floor", department_result.get("floor", 0))),
                float(item.get("x", department_result.get("x", 0.0))),
                float(item.get("y", department_result.get("y", 0.0))),
            )
            created += 1

        department_result.pop("_create_locations", None)
        return created

    def on_floor_changed(self, *_):
        self.refresh_canvas()
        self._queue_all_floor_dxf_loads(
            active_floor=self.floor_spin.value(), force_reload=False
        )

    def floor_dxf_entries(self):
        return self.store.data.setdefault("floor_dxf_files", [])

    def get_floor_dxf_path(self, floor):
        for entry in self.floor_dxf_entries():
            try:
                if int(entry.get("floor")) == int(floor):
                    path = (entry.get("filepath") or "").strip()
                    return path or None
            except Exception:
                continue
        return None

    def set_floor_dxf_path(self, floor, filepath):
        entries = self.floor_dxf_entries()
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

    def clear_floor_dxf_mapping(self, floor):
        self.store.data["floor_dxf_files"] = [
            entry
            for entry in self.floor_dxf_entries()
            if int(entry.get("floor", -(10**9))) != int(floor)
        ]

    def _all_mapped_floors(self):
        floors = []
        for entry in self.floor_dxf_entries():
            try:
                floor = int(entry.get("floor"))
            except Exception:
                continue
            if self.get_floor_dxf_path(floor):
                floors.append(floor)
        return sorted(set(floors))

    def _ensure_loading_dialog(self):
        if self._loading_dialog is None:
            self._loading_dialog = DXFLoadingDialog(self)
        return self._loading_dialog

    def _update_loading_dialog(self):
        if not self._loading_batch_active:
            return
        dialog = self._ensure_loading_dialog()
        total = len(self._loading_batch_floors)
        completed = 0
        for floor in self._loading_batch_floors:
            path = self.get_floor_dxf_path(floor)
            cached = self._dxf_cache.get(floor)
            if cached and path and cached.get("path") == path:
                completed += 1
            elif floor in self._loading_batch_failed:
                completed += 1
        failed_count = len(self._loading_batch_failed)
        pending = max(0, total - completed)
        message = f"Loading {total} DXF file(s)..."
        if pending:
            message = f"Loading {pending} remaining DXF file(s)..."
        elif failed_count:
            message = "Finished loading DXFs with some failures."
        else:
            message = "Finished loading all DXFs."
        dialog.update_progress(completed, total, message, failed_count=failed_count)
        if total > 0 and not dialog.isVisible():
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        QApplication.processEvents()
        if total > 0 and completed >= total:
            dialog.mark_complete()
            self._loading_batch_active = False

    def _start_loading_batch(self, floors):
        target_floors = []
        for floor in floors:
            floor = int(floor)
            path = self.get_floor_dxf_path(floor)
            if path:
                target_floors.append(floor)
        target_floors = sorted(set(target_floors))
        if not target_floors:
            return
        self._loading_batch_floors = set(target_floors)
        self._loading_batch_failed = set()
        self._loading_batch_active = True
        dialog = self._ensure_loading_dialog()
        dialog._completed = False
        self._update_loading_dialog()

    def _queue_all_floor_dxf_loads(self, active_floor=None, force_reload=False):
        floors = self._all_mapped_floors()
        if not floors:
            return

        if active_floor is not None:
            active_floor = int(active_floor)
            floors = [active_floor] + [
                int(floor) for floor in floors if int(floor) != active_floor
            ]

        jobs = []

        for floor in floors:
            floor = int(floor)
            path = self.get_floor_dxf_path(floor)
            if not path:
                continue

            cached = self._dxf_cache.get(floor)
            force_this = bool(
                force_reload and active_floor is not None and floor == int(active_floor)
            )

            if (not force_this) and cached and cached.get("path") == path:
                if active_floor is not None and floor == int(active_floor):
                    self._set_active_dxf_floor(floor)
                continue

            if floor in self._dxf_loading_floors:
                continue

            self._dxf_loading_floors.add(floor)
            jobs.append((floor, path))

        if not jobs:
            self._update_loading_dialog()
            return

        self._start_loading_batch([floor for floor, _path in jobs])

        if active_floor is not None:
            self.set_status("Loading DXFs using multiple processes...")

        self._request_dxf_batch_load.emit(jobs)
        self._update_loading_dialog()

    def _clear_dxf_cache(self):
        self._dxf_cache.clear()
        self._dxf_loading_floors.clear()
        self._loading_batch_floors.clear()
        self._loading_batch_failed.clear()
        self._loading_batch_active = False
        if self._loading_dialog is not None and self._loading_dialog.isVisible():
            self._loading_dialog.mark_complete()
        self.current_dxf_path = None
        self.loaded_dxf_floor = None
        self.dxf_scene.clear()

    def _set_active_dxf_floor(self, floor):
        floor = int(floor)
        cached = self._dxf_cache.get(floor)
        if not cached:
            self.current_dxf_path = None
            self.loaded_dxf_floor = None
            self.dxf_scene.clear()
            return False
        self.current_dxf_path = cached["path"]
        self.loaded_dxf_floor = floor
        self.dxf_scene.set_content(cached["path"], cached["entities"], cached["bounds"])
        return True

    def request_floor_dxf_load(self, floor, force_reload=False, prefetch=False):
        floor = int(floor)
        path = self.get_floor_dxf_path(floor)

        if not path:
            if not prefetch and floor == self.floor_spin.value():
                self.dxf_scene.clear()
                self.current_dxf_path = None
                self.loaded_dxf_floor = None
            return False

        cached = self._dxf_cache.get(floor)
        if (not force_reload) and cached and cached.get("path") == path:
            if not prefetch and floor == self.floor_spin.value():
                self._set_active_dxf_floor(floor)
            return True

        if floor in self._dxf_loading_floors:
            self._update_loading_dialog()
            return False

        self._dxf_loading_floors.add(floor)
        self._start_loading_batch([floor])

        if not prefetch and floor == self.floor_spin.value():
            self.set_status(f"Loading DXF for floor {floor}...")

        self._request_dxf_batch_load.emit([(floor, path)])
        self._update_loading_dialog()
        return False

    def ensure_floor_dxf_loaded(self, floor, force_reload=False):
        floor = int(floor)
        path = self.get_floor_dxf_path(floor)
        if not path:
            if floor == self.floor_spin.value():
                self.dxf_scene.clear()
                self.current_dxf_path = None
                self.loaded_dxf_floor = None
            return False

        cached = self._dxf_cache.get(floor)
        if (not force_reload) and cached and cached.get("path") == path:
            return self._set_active_dxf_floor(floor)

        self.request_floor_dxf_load(floor, force_reload=force_reload, prefetch=False)
        return False

    def _prefetch_other_floor_dxfs(self, active_floor):
        for entry in self.floor_dxf_entries():
            try:
                floor = int(entry.get("floor"))
            except Exception:
                continue
            if floor == int(active_floor):
                continue
            self.request_floor_dxf_load(floor, prefetch=True)

    @Slot(int, str, object, object)
    def _on_dxf_loaded(self, floor, path, entities, bounds):
        floor = int(floor)
        self._dxf_loading_floors.discard(floor)
        self._loading_batch_failed.discard(floor)
        self._dxf_cache[floor] = {
            "path": path,
            "entities": list(entities or []),
            "bounds": bounds,
        }

        if self.get_floor_dxf_path(floor) != path:
            return

        if floor == self.floor_spin.value():
            self._set_active_dxf_floor(floor)
            self.refresh_canvas()
            if self._pending_fit_after_load:
                self._pending_fit_after_load = False
                self.fit_view()
            else:
                self.set_status(f"Loaded DXF {Path(path).name} for floor {floor}")

        self._prefetch_other_floor_dxfs(floor)
        self._update_loading_dialog()

    @Slot(int, str, str)
    def _on_dxf_failed(self, floor, path, error):
        floor = int(floor)
        self._dxf_loading_floors.discard(floor)
        self._loading_batch_failed.add(floor)
        cached = self._dxf_cache.get(floor)
        if cached and cached.get("path") == path:
            self._dxf_cache.pop(floor, None)
        if floor == self.floor_spin.value():
            self.dxf_scene.clear()
            self.current_dxf_path = None
            self.loaded_dxf_floor = None
            self.refresh_canvas()
            self.set_status(f"Failed to load DXF for floor {floor}: {error}")
        self._update_loading_dialog()

    def world_to_scene(self, x, y):
        return QPointF(float(x), -float(y))

    def scene_to_world(self, sx, sy):
        return float(sx), -float(sy)

    def snap(self, x, y):
        if self.snap_check.isChecked():
            return round(x), round(y)
        return round(x, 3), round(y, 3)

    def _content_bounds(self, floor):
        bounds = []
        if self.dxf_scene.bounds and self.loaded_dxf_floor == int(floor):
            bounds.append(self.dxf_scene.bounds)

        floor_points = self.store.points_for_floor(floor)
        if floor_points:
            xs = [float(p["x"]) for p in floor_points.values()]
            ys = [float(p["y"]) for p in floor_points.values()]
            bounds.append((min(xs), min(ys), max(xs), max(ys)))

        if not bounds:
            return None

        min_x = min(b[0] for b in bounds)
        min_y = min(b[1] for b in bounds)
        max_x = max(b[2] for b in bounds)
        max_y = max(b[3] for b in bounds)
        return min_x, min_y, max_x, max_y

    def _scene_rect_for_floor(self, floor, padding=8.0):
        bounds = self._content_bounds(floor)
        if not bounds:
            return None
        min_x, min_y, max_x, max_y = bounds
        return self._scene_rect_from_bounds(
            (min_x, min_y, max_x, max_y), padding=padding
        )

    def _scene_rect_from_bounds(self, bounds, padding=8.0):
        min_x, min_y, max_x, max_y = bounds
        return QRect(
            min_x - padding,
            -(max_y + padding),
            max(1.0, (max_x - min_x) + (padding * 2)),
            max(1.0, (max_y - min_y) + (padding * 2)),
        )

    def fit_view(self):
        floor = self.floor_spin.value()
        ready = self.ensure_floor_dxf_loaded(floor)
        rect = self._scene_rect_for_floor(floor, padding=8.0)
        if rect is None and not ready and self.get_floor_dxf_path(floor):
            self._pending_fit_after_load = True
            return
        if (
            rect is not None
            and not rect.isNull()
            and rect.width() > 0
            and rect.height() > 0
        ):
            self.canvas.resetTransform()
            self.canvas.fitInView(rect, Qt.KeepAspectRatio)
            self.scene.setSceneRect(rect.adjusted(-40, -40, 40, 40))
            self.canvas.viewport().update()
        self.refresh_canvas()

    def refresh_canvas(self):
        self.scene.clear()
        self._item_lookup = {}
        self._point_item_lookup = {}
        floor = self.floor_spin.value()
        self.ensure_floor_dxf_loaded(floor)
        self.scene.setBackgroundBrush(QBrush(QColor("#111111")))
        rect = self._scene_rect_for_floor(floor, padding=8.0)
        if rect is not None:
            self.scene.setSceneRect(rect.adjusted(-40, -40, 40, 40))
        if (
            self.show_dxf_check.isChecked()
            and self.loaded_dxf_floor == int(floor)
            and self.dxf_scene.entities
        ):
            self.dxf_scene.populate_graphics_scene(
                self.scene, self.canvas.transform().m11()
            )
        if self.show_location_bounds_check.isChecked():
            self.draw_location_bounding_boxes(floor)
        self.draw_temporary_location_bounding_box(floor)
        self.draw_edges(floor)
        self.draw_points(floor)
        if self.show_charging_spaces_check.isChecked():
            self.draw_charging_space_markers(floor)
        self.file_label.setText(self.current_json_path or "New file")
        self.canvas.viewport().update()

    def draw_charging_space_markers(self, floor):
        """Overlay a compact charger count beside locations with charger-equipped AMR bays."""
        for location in self.store.locations_for_floor(floor):
            spaces = list(location.get("inventory_spaces", []) or [])
            chargers = [
                space for space in spaces
                if isinstance(space, dict)
                and bool(space.get("stores_amr", False) or str(space.get("space_type", "")).lower() == "amr")
                and bool(space.get("has_charger", False))
            ]
            if not chargers:
                continue
            point = self.world_to_scene(float(location.get("x", 0.0)), float(location.get("y", 0.0)))
            label = self.scene.addText(f"⚡ {len(chargers)}")
            label.setDefaultTextColor(QColor("#ffd54f"))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setPos(point.x() + 0.35, point.y() - 0.75)
            label.setZValue(40)

    def _edge_rows_for_point(self, point_name):
        points = self.store.all_points()
        results = []
        for edge in self.store.data.get("corridors", {}).get("edges", []):
            if edge.get("from") != point_name and edge.get("to") != point_name:
                continue
            from_name = edge.get("from", "")
            to_name = edge.get("to", "")
            from_point = points.get(from_name)
            to_point = points.get(to_name)
            from_floor = from_point.get("floor", "") if from_point else ""
            to_floor = to_point.get("floor", "") if to_point else ""
            results.append(
                {
                    "from": from_name,
                    "from_floor": from_floor,
                    "to": to_name,
                    "to_floor": to_floor,
                    "cross_floor": (
                        from_point is not None
                        and to_point is not None
                        and int(from_floor) != int(to_floor)
                    ),
                }
            )
        results.sort(
            key=lambda edge: (
                str(edge.get("from_floor", "")),
                str(edge.get("from", "")),
                str(edge.get("to_floor", "")),
                str(edge.get("to", "")),
            )
        )
        return results

    def _delete_edge_connections(self, edges):
        removed = 0
        for edge in edges:
            before = len(self.store.data.get("corridors", {}).get("edges", []))
            self.store.remove_edge(edge.get("from", ""), edge.get("to", ""))
            after = len(self.store.data.get("corridors", {}).get("edges", []))
            if after < before:
                removed += 1
        self.refresh_canvas()
        self.set_status(f"Deleted {removed} edge connection(s)")

    def _show_edge_connections_dialog(self, point_name):
        dialog = EdgeConnectionsDialog(
            self,
            point_name,
            self._edge_rows_for_point(point_name),
            self._delete_edge_connections,
        )
        dialog.exec()

    def find_nearest_bounding_box_point_index(self, x, y, radius_world=0.6):
        if not self.bounding_box_location_name:
            return None

        best_index = None
        best_dist = radius_world

        for idx, point in enumerate(self.bounding_box_points):
            d = math.hypot(float(point["x"]) - x, float(point["y"]) - y)
            if d <= best_dist:
                best_index = idx
                best_dist = d

        return best_index

    def draw_location_bounding_boxes(self, floor):
        pen = QPen(QColor("#00e5ff"), 0)
        brush = QBrush(QColor(0, 229, 255, 35))
        for location in self.store.locations_for_floor(floor):
            points = self.store.get_location_bounding_box_points(location["name"]) or []
            if len(points) < 3:
                continue
            poly = QPolygonF([self.world_to_scene(p["x"], p["y"]) for p in points])
            item = QGraphicsPolygonItem(poly)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(-5)
            self.scene.addItem(item)
            self._item_lookup[item] = (
                "location_bounding_box",
                location.get("name", ""),
            )

    def draw_temporary_location_bounding_box(self, floor):
        if self._current_edit_mode() != "location_bbox":
            return
        if not self.bounding_box_location_name or not self.bounding_box_points:
            return
        location = self.store.get_location(self.bounding_box_location_name)
        if not location or int(location.get("floor", -1)) != int(floor):
            return

        pts = [self.world_to_scene(p["x"], p["y"]) for p in self.bounding_box_points]
        pen = QPen(QColor("#ffdd57"), 0)
        pen.setStyle(Qt.DashLine)
        brush = QBrush(QColor(255, 221, 87, 35)) if len(pts) >= 3 else Qt.NoBrush

        # if len(pts) == 1:
        #     p = pts[0]
        #     self.scene.addEllipse(p.x() - 0.15, p.y() - 0.15, 0.3, 0.3, pen, QBrush(QColor("#ffdd57")))
        #     return

        poly = QPolygonF(pts)
        item = QGraphicsPolygonItem(poly)
        item.setPen(pen)
        item.setBrush(brush)
        item.setZValue(-4)
        self.scene.addItem(item)

        for idx, p in enumerate(pts):
            handle_pen = QPen(QColor("#ffffff"), 0)
            handle_brush = QBrush(
                QColor("#ff6b6b")
                if idx == self.dragging_bounding_box_point_index
                else QColor("#ffdd57")
            )
            handle = self.scene.addEllipse(
                p.x() - 0.18,
                p.y() - 0.18,
                0.36,
                0.36,
                handle_pen,
                handle_brush,
            )
            handle.setZValue(20)
            self._item_lookup[handle] = ("location_bounding_box_point", idx)

    def start_location_bounding_box_draw(self, location_name, keep_existing=False):
        location = self.store.get_location(location_name)
        if location is None:
            return
        self.bounding_box_location_name = location_name
        existing = self.store.get_location_bounding_box_points(location_name)
        self.bounding_box_points = [dict(p) for p in existing] if keep_existing else []
        self._set_edit_mode("location_bbox")
        self.show_location_bounds_check.setChecked(True)
        self.set_status(
            f"Drawing bounding box for {location_name}. Left-click points, right-click empty space to finish."
        )
        self.refresh_canvas()

    def finish_location_bounding_box_draw(self):
        name = self.bounding_box_location_name
        if not name:
            return
        if len(self.bounding_box_points) < 3:
            QMessageBox.critical(
                self,
                "Bounding box",
                "Add at least three points for a room bounding box.",
            )
            return
        self.store.set_location_bounding_box(name, self.bounding_box_points)
        self.bounding_box_location_name = None
        self.bounding_box_points = []
        self._set_edit_mode("select_move")
        self.set_status(f"Saved bounding box for {name}")
        self.refresh_canvas()

    def cancel_location_bounding_box_draw(self):
        name = self.bounding_box_location_name
        self.bounding_box_location_name = None
        self.bounding_box_points = []
        self.set_status(
            f"Cancelled bounding box drawing for {name}"
            if name
            else "Bounding box drawing cancelled"
        )
        self.refresh_canvas()

    def draw_edges(self, floor):
        points = self.store.all_points()
        colour_by_area = {
            "none": QColor("#6aa9ff"),
            "staff": QColor("#4fc3f7"),
            "public": QColor("#ffb74d"),
            "both": QColor("#ba68c8"),
        }
        pen_cross_floor = QPen(QColor("#ff4d4f"), 0)
        for edge in self.store.data.get("corridors", {}).get("edges", []):
            a = points.get(edge["from"])
            b = points.get(edge["to"])
            if not a or not b:
                continue
            a_floor = int(a["floor"])
            b_floor = int(b["floor"])
            if int(floor) not in {a_floor, b_floor}:
                continue
            pa = self.world_to_scene(a["x"], a["y"])
            pb = self.world_to_scene(b["x"], b["y"])
            key = self._physical_edge_key(edge.get("from"), edge.get("to"))
            if key in self.selected_edge_keys:
                pen = QPen(QColor("#00e5ff"), 0.12)
            elif a_floor != b_floor:
                pen = pen_cross_floor
            else:
                area = str(edge.get("people_area_type", "none") or "none").lower()
                pen = QPen(colour_by_area.get(area, colour_by_area["none"]), 0)
            item = self.scene.addLine(pa.x(), pa.y(), pb.x(), pb.y(), pen)
            item.setZValue(5 if key in self.selected_edge_keys else 0)
            self._item_lookup[item] = ("edge", edge)

    def draw_points(self, floor):
        for name, point in self.store.points_for_floor(floor).items():
            pos = self.world_to_scene(point["x"], point["y"])
            selected = name in self.selected_point_names or name == self.selected_point_name
            route_selected = (
                self.route_profile_selection_active
                and name in self.route_profile_selected_nodes
            )
            route_allowed = (
                not self.route_profile_selection_active
                or name in self.route_profile_allowed_point_names
            )
            kind = point.get("kind")
            outline = QPen(
                (
                    QColor("#00e5ff")
                    if route_selected or selected
                    else QColor("transparent")
                ),
                0.12 if selected else 0,
            )
            if kind == "location":
                r = 0.3
                item = self.scene.addEllipse(
                    pos.x() - r,
                    pos.y() - r,
                    2 * r,
                    2 * r,
                    outline,
                    QBrush(QColor("#18c37e")),
                )
                label_color = QColor("#9bf0cd")
            elif kind == "corridor_node":
                r = 0.3
                has_door = bool(point.get("has_door", False))
                item = self.scene.addRect(
                    pos.x() - r,
                    pos.y() - r,
                    2 * r,
                    2 * r,
                    outline,
                    QBrush(QColor("#ff8f00") if has_door else QColor("#f2c94c")),
                )
                label_color = QColor("#ffd180") if has_door else QColor("#ffe8a3")
                if has_door:
                    door_text = QGraphicsSimpleTextItem("D")
                    door_text.setBrush(QBrush(QColor("#ffffff")))
                    door_text.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
                    door_text.setPos(pos.x() - 0.12, pos.y() - 0.55)
                    door_text.setZValue(35)
                    self.scene.addItem(door_text)
                    self._item_lookup[door_text] = ("point_label", name)
            elif kind == "department":
                poly = QPolygonF(
                    [
                        QPointF(pos.x(), pos.y() - 0.9),
                        QPointF(pos.x() + 0.9, pos.y()),
                        QPointF(pos.x(), pos.y() + 0.9),
                        QPointF(pos.x() - 0.9, pos.y()),
                    ]
                )
                item = QGraphicsPolygonItem(poly)
                item.setBrush(QBrush(QColor("#14b8a6")))
                item.setPen(outline if selected else QPen(QColor("#7be7dc"), 0.08))
                self.scene.addItem(item)
                label_color = QColor("#bff7f2")
            else:
                r = 0.3
                poly = [
                    QPointF(pos.x(), pos.y() - r),
                    QPointF(pos.x() + r, pos.y()),
                    QPointF(pos.x(), pos.y() + r),
                    QPointF(pos.x() - r, pos.y()),
                ]
                item = QGraphicsPolygonItem()
                item.setPolygon(QPolygonF(poly))
                item.setPen(outline)
                item.setBrush(QBrush(QColor("#ff7b72")))
                self.scene.addItem(item)
                label_color = QColor("#ffb3ae")
            if not route_allowed:
                item.setOpacity(0.3)
            item.setFlag(QGraphicsItem.ItemIgnoresTransformations, False)
            item.setZValue(30 if route_selected or selected else 10)
            self._item_lookup[item] = ("point", name)
            self._point_item_lookup[name] = item
            if self.show_labels_check.isChecked():
                text = QGraphicsSimpleTextItem(name)
                text.setBrush(label_color)
                text.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
                text.setPos(pos.x() + 0.5, pos.y())
                self.scene.addItem(text)
                self._item_lookup[text] = ("point_label", name)

    def draw_overlay_panels(self, painter, viewport_rect):
        floor = self.floor_spin.value()
        mapped_path = self.get_floor_dxf_path(floor)
        dxf_name = Path(mapped_path).name if mapped_path else "None"
        lines = [
            "Legend",
            "Green circle = location",
            "Yellow square = corridor node",
            "Orange D square = door node",
            "Blue/orange/purple edge = staff/public/mixed",
            "Teal diamond = department",
            "Red diamond = lift node",
            f"Mode: {self.mode_combo.currentText()} | Floor: {floor}",
            f"DXF: {dxf_name}",
            "Double-click a point to edit",
            "Ctrl-click nodes/edges for multiple selection",
            f"Selected: {len(self.selected_point_names)} nodes, {len(self.selected_edge_keys)} corridors",
        ]
        if self._current_edit_mode() == "department":
            lines.append("Click anywhere to add a department")
        if self._current_edit_mode() == "location_bbox":
            if self.bounding_box_location_name:
                lines.append(
                    f"Bounding: {self.bounding_box_location_name} ({len(self.bounding_box_points)} pts)"
                )
                lines.append("Left-click = add point | Right-click empty = finish")
            else:
                lines.append("Right-click a location and choose draw bounding box")
        self._draw_overlay_box(painter, 12, 12, 320, lines, "#333333", "white")

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

    def find_nearest_point_name(self, x, y, floor, radius_world=3.0):
        best = None
        best_dist = radius_world
        for name, point in self.store.points_for_floor(floor).items():
            d = math.hypot(point["x"] - x, point["y"] - y)
            if d <= best_dist:
                best = name
                best_dist = d
        return best

    def _item_at_scene(self, sx, sy):
        return self.canvas.itemAt(self.canvas.mapFromScene(QPointF(sx, sy)))

    def create_department_at_point(self, point_name):
        point = self.store.all_points().get(point_name)
        if not point:
            return

        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))
        waste_stream_names = sorted(
            x["name"] for x in self.store.data.get("waste_streams", [])
        )

        dialog = DepartmentEditorDialog(
            self,
            location_names=location_names,
            waste_stream_names=waste_stream_names,
            current_floor=self.floor_spin.value(),
            default_department_id=self.store.suggest_next_department_id(),
            default_x=point["x"],
            default_y=point["y"],
            group_resolver=lambda item: f"Floor {self.build_floor_map(self.store.data).get(item, 'Other')}",
            task_generation_categories=self.task_generation_category_pairs(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            if dialog.result["name"] in self.store.names_in_use():
                QMessageBox.critical(
                    self, "Duplicate name", "Department name already exists"
                )
                return
            created_locations = self.create_department_generated_locations(
                dialog.result
            )
            self.store.upsert_department(dialog.result)
            self.set_status(f"Added department {dialog.result['name']}")
            self.refresh_canvas()

    def create_department_at_position(self, x, y, floor):
        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))
        waste_stream_names = sorted(
            x["name"] for x in self.store.data.get("waste_streams", [])
        )

        dialog = DepartmentEditorDialog(
            self,
            location_names=location_names,
            waste_stream_names=waste_stream_names,
            current_floor=floor,
            default_department_id=self.store.suggest_next_department_id(),
            default_x=x,
            default_y=y,
            group_resolver=lambda item: f"Floor {self.build_floor_map(self.store.data).get(item, 'Other')}",
            task_generation_categories=self.task_generation_category_pairs(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            dept_name = str(dialog.result.get("name", "")).strip()
            if dept_name in self.store.names_in_use():
                QMessageBox.critical(
                    self, "Duplicate name", "Department name already exists"
                )
                return
            created_locations = self.create_department_generated_locations(
                dialog.result
            )
            self.store.upsert_department(dialog.result)
            self.selected_point_name = dept_name
            self.set_status(f"Added department {dept_name}")
            self.refresh_canvas()

    def edit_department_by_name(self, dept_name):
        dept = next(
            (
                x
                for x in self.store.data.get("departments", [])
                if str(x.get("name", "")).strip() == str(dept_name).strip()
            ),
            None,
        )
        if not dept:
            return

        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))
        waste_stream_names = sorted(
            x["name"] for x in self.store.data.get("waste_streams", [])
        )

        dialog = DepartmentEditorDialog(
            self,
            location_names=location_names,
            waste_stream_names=waste_stream_names,
            current_floor=self.floor_spin.value(),
            seed=dept,
            default_department_id=str(dept.get("id", "")),
            default_x=float(dept.get("x", 0.0)),
            default_y=float(dept.get("y", 0.0)),
            group_resolver=lambda item: f"Floor {self.build_floor_map(self.store.data).get(item, 'Other')}",
            task_generation_categories=self.task_generation_category_pairs(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result:
            for other in self.store.data.get("departments", []):
                if other is dept:
                    continue
                if (
                    str(other.get("id", "")).strip()
                    == str(dialog.result.get("id", "")).strip()
                ):
                    QMessageBox.critical(
                        self, "Duplicate", "Department ID already exists"
                    )
                    return
                if (
                    str(other.get("name", "")).strip()
                    == str(dialog.result.get("name", "")).strip()
                ):
                    QMessageBox.critical(
                        self, "Duplicate", "Department name already exists"
                    )
                    return

            old_name = str(dept.get("name", "")).strip()
            new_name = str(dialog.result.get("name", "")).strip()

            created_locations = self.create_department_generated_locations(
                dialog.result
            )
            self.store.upsert_department(dialog.result)

            if old_name and new_name and old_name != new_name:
                self.store.rename_point(old_name, new_name)

            self.set_status(f"Edited department {new_name}")
            self.refresh_canvas()

    def on_left_click(self, event, sx, sy):
        mode = self._current_edit_mode()
        floor = self.floor_spin.value()
        raw_x, raw_y = self.scene_to_world(sx, sy)
        x, y = self.snap(raw_x, raw_y)

        if self.department_location_placement_active:
            self.finish_department_location_placement(x, y, floor)
            return

        if self.route_profile_selection_active:
            picked = self._route_profile_pickable_point_at(raw_x, raw_y, floor)

            if picked:
                if not (event.modifiers() & Qt.ControlModifier):
                    self.route_profile_selected_nodes.clear()

                if picked in self.route_profile_selected_nodes:
                    self.route_profile_selected_nodes.remove(picked)
                else:
                    self.route_profile_selected_nodes.add(picked)

                self.set_status(
                    f"Route profile selection: {len(self.route_profile_selected_nodes)} node(s)"
                )
                self.refresh_canvas()

            return

        if mode == "pan":
            self.last_pan = event.position().toPoint()
            return

        picked = self.find_nearest_point_name(raw_x, raw_y, floor)
        picked_edge = None if picked else self.find_nearest_edge_key(raw_x, raw_y, floor)

        if mode == "select_move":
            additive = bool(
                event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
            )
            self.dragging_point_name = None
            self.drag_mode_active = False
            if picked:
                if not additive:
                    self.selected_point_names.clear()
                    self.selected_edge_keys.clear()
                if additive and picked in self.selected_point_names:
                    self.selected_point_names.remove(picked)
                    self.selected_point_name = None
                else:
                    self.selected_point_names.add(picked)
                    self.selected_point_name = picked
                    if not additive:
                        self.dragging_point_name = picked
                        self.drag_mode_active = True
                self.set_status(
                    f"Selected {len(self.selected_point_names)} node(s) and "
                    f"{len(self.selected_edge_keys)} corridor(s)"
                )
            elif picked_edge is not None:
                if not additive:
                    self.selected_point_names.clear()
                    self.selected_edge_keys.clear()
                    self.selected_point_name = None
                if additive and picked_edge in self.selected_edge_keys:
                    self.selected_edge_keys.remove(picked_edge)
                else:
                    self.selected_edge_keys.add(picked_edge)
                self.set_status(
                    f"Selected {len(self.selected_point_names)} node(s) and "
                    f"{len(self.selected_edge_keys)} corridor(s)"
                )
            elif not additive:
                self.selected_point_names.clear()
                self.selected_edge_keys.clear()
                self.selected_point_name = None
                self.set_status("Selection cleared")
            self.refresh_canvas()
            return

        self.selected_point_name = picked

        if mode == "delete":
            if picked:
                if picked.startswith("Lift-") and "-F" in picked:
                    lift_id = picked.rsplit("-F", 1)[0]
                    if (
                        QMessageBox.question(
                            self, "Delete lift", f"Delete entire {lift_id}?"
                        )
                        == QMessageBox.Yes
                    ):
                        self.store.delete_lift(lift_id)
                        self.selected_point_name = None
                        self.set_status(f"Deleted {lift_id}")
                else:
                    if (
                        QMessageBox.question(self, "Delete point", f"Delete {picked}?")
                        == QMessageBox.Yes
                    ):
                        cleanup = self.store.delete_point(picked)
                        self.selected_point_name = None
                        removed_refs = 0
                        if isinstance(cleanup, dict):
                            removed_refs = sum(
                                int(cleanup.get(key, 0) or 0)
                                for key in (
                                    "department_references_removed",
                                    "task_generation_references_removed",
                                    "building_references_removed",
                                    "mass_collection_references_removed",
                                )
                            )
                        suffix = (
                            f" and removed {removed_refs} redundant reference(s)"
                            if removed_refs
                            else ""
                        )
                        self.set_status(f"Deleted {picked}{suffix}")
                self.refresh_canvas()
            return

        if mode == "corridor_node":
            name = self.store.suggest_next_corridor_name(floor)
            self.store.add_corridor_node(name, floor, x, y)
            self.selected_point_name = name
            self.set_status(f"Added corridor node {name}")
            self.refresh_canvas()
            return

        if mode == "location":
            name, ok = QInputDialog.getText(self, "Location", "Location name:")
            if not ok or not name:
                return
            self.store.add_location(name, floor, x, y)
            self.set_status(f"Added location {name}")
            self.refresh_canvas()
            return

        if mode == "location_bbox":
            if not self.bounding_box_location_name:
                if picked:
                    picked_point = self.store.all_points().get(picked, {})
                    if picked_point.get("kind") == "location":
                        self.start_location_bounding_box_draw(
                            picked, keep_existing=False
                        )
                    else:
                        self.set_status("Pick a location before drawing a bounding box")
                else:
                    self.set_status(
                        "Right-click a location and choose draw bounding box, or left-click a location first"
                    )
                return

            location = self.store.get_location(self.bounding_box_location_name)
            if not location or int(location.get("floor", -1)) != int(floor):
                self.set_status("Bounding box location is not on the current floor")
                return

            hit_index = self.find_nearest_bounding_box_point_index(x, y)
            if hit_index is not None:
                self.dragging_bounding_box_point_index = hit_index
                self.set_status(
                    f"Dragging bounding box point {hit_index + 1} for {self.bounding_box_location_name}"
                )
                self.refresh_canvas()
                return

            self.bounding_box_points.append({"x": x, "y": y})
            self.set_status(
                f"Added point {len(self.bounding_box_points)} for {self.bounding_box_location_name}"
            )
            self.refresh_canvas()
            return

        if mode == "department":
            existing = self.find_nearest_point_name(x, y, floor)
            if existing:
                point = self.store.all_points().get(existing, {})
                if point.get("kind") == "department":
                    self.edit_department_by_name(existing)
                    return

            self.create_department_at_position(x, y, floor)
            return

        if mode == "edge":
            if not picked:
                self.set_status("No nearby point found")
                return

            picked_point = self.store.all_points().get(picked, {})
            if picked_point.get("kind") == "department":
                self.set_status("Departments cannot be connected by corridor edges")
                return

            if self.selected_for_edge is None:
                self.selected_for_edge = picked
                self.set_status(f"Edge start selected: {picked}")
            else:
                start_point = self.store.all_points().get(self.selected_for_edge, {})
                if start_point.get("kind") == "department":
                    self.set_status("Departments cannot be connected by corridor edges")
                    self.selected_for_edge = None
                    return

                start = self.selected_for_edge

                self.store.add_edge(start, picked)
                if self.bidirectional_check.isChecked():
                    self.store.add_edge(picked, start)

                if self.chain_edges_check.isChecked():
                    self.selected_for_edge = picked
                    self.set_status(
                        f"Connected {start} -> {picked}. Chain start now: {picked}"
                    )
                else:
                    self.selected_for_edge = None
                    self.set_status(f"Connected {start} -> {picked}")

                self.refresh_canvas()

        if mode == "lift":
            existing_lift = None
            if picked and picked.startswith("Lift-") and "-F" in picked:
                lift_id = picked.rsplit("-F", 1)[0]
                for item in self.store.data.get("lifts", []):
                    if item["id"] == lift_id:
                        existing_lift = item
                        break
            dialog = LiftEditorDialog(
                self, existing_lift, default_floor=floor, default_x=x, default_y=y
            )
            if dialog.exec() == QDialog.Accepted and dialog.result:
                self._save_lift_result(dialog.result, old_id=existing_lift.get("id") if existing_lift else None)
                self.set_status(f"Saved {dialog.result['id']}")
                self.refresh_canvas()
            return

    def on_double_click(self, event, sx, sy):
        floor = self.floor_spin.value()
        x, y = self.scene_to_world(sx, sy)
        picked = self.find_nearest_point_name(x, y, floor)
        if not picked:
            return
        point = self.store.all_points()[picked]
        if point.get("kind") == "lift_node":
            lift_id = point["lift_id"]
            existing_lift = next(
                (x for x in self.store.data.get("lifts", []) if x["id"] == lift_id),
                None,
            )
            dialog = LiftEditorDialog(
                self,
                existing_lift,
                default_floor=floor,
                default_x=point["x"],
                default_y=point["y"],
            )
            if dialog.exec() == QDialog.Accepted and dialog.result:
                self._save_lift_result(dialog.result, old_id=existing_lift.get("id") if existing_lift else None)
                self.set_status(f"Edited {dialog.result['id']}")
                self.refresh_canvas()
            return
        if point.get("kind") == "department":
            self.edit_department_by_name(picked)
            return
        dialog = PointEditorDialog(self, f"Edit {picked}", picked, point)
        if dialog.exec() == QDialog.Accepted and dialog.result:
            self.store.set_point_position(
                picked, dialog.result["x"], dialog.result["y"]
            )
            if point.get("kind") == "location":
                location = self.store.get_location(picked)
                if location is not None:
                    for key in ("wash_cycle_required", "wash_cycle_duration_sec", "wash_location", "people_area_type"):
                        if key in dialog.result:
                            location[key] = dialog.result[key]
            elif point.get("kind") == "corridor_node":
                for node in self.store.data.get("corridors", {}).get("nodes", []):
                    if node.get("name") == picked:
                        for key in ("has_door", "door_clear_width_m"):
                            if key in dialog.result:
                                node[key] = dialog.result[key]
                        break
            self.store.rename_point(picked, dialog.result["name"])
            if picked in self.selected_point_names:
                self.selected_point_names.remove(picked)
                self.selected_point_names.add(dialog.result["name"])
            self.selected_point_name = dialog.result["name"]
            self.set_status(f"Edited {dialog.result['name']}")
            self.refresh_canvas()

    def on_left_release(self, event):
        if self.route_profile_selection_active:
            if self.route_profile_selection_rect_item is not None:
                rect = self.route_profile_selection_rect_item.rect()
                keep_existing = bool(event.modifiers() & Qt.ControlModifier)

                if not keep_existing:
                    self.route_profile_selected_nodes.clear()

                floor = self.floor_spin.value()

                for name, point in self.store.points_for_floor(floor).items():
                    if name not in self.route_profile_allowed_point_names:
                        continue

                    pos = self.world_to_scene(point["x"], point["y"])
                    if rect.contains(pos):
                        self.route_profile_selected_nodes.add(name)

                self.scene.removeItem(self.route_profile_selection_rect_item)
                self.route_profile_selection_rect_item = None
                self.route_profile_selection_rect_start = None

                self.set_status(
                    f"Route profile selection: {len(self.route_profile_selected_nodes)} node(s)"
                )
                self.refresh_canvas()

            return

        self.dragging_point_name = None
        self.drag_mode_active = False
        self.dragging_bounding_box_point_index = None
        self.last_pan = None

    def on_right_click(self, event, sx, sy):
        mode = self._current_edit_mode()
        floor = self.floor_spin.value()
        x, y = self.scene_to_world(sx, sy)
        picked = self.find_nearest_point_name(x, y, floor)
        picked_edge = None if picked else self.find_nearest_edge_key(x, y, floor)

        if self.department_location_placement_active:
            self.cancel_department_location_placement()
            return

        if self.route_profile_selection_active:
            if picked:
                return

            menu = QMenu(self)
            finish_action = menu.addAction("Apply route profile selection")
            cancel_action = menu.addAction("Cancel route profile selection")

            action = menu.exec(event.globalPosition().toPoint())

            if action == finish_action:
                self._finish_route_profile_graphical_selection()
            elif action == cancel_action:
                self._cancel_route_profile_graphical_selection()

            return

        if mode == "select_move":
            if picked and picked not in self.selected_point_names:
                self.selected_point_names = {picked}
                self.selected_edge_keys.clear()
                self.selected_point_name = picked
            elif picked_edge is not None and picked_edge not in self.selected_edge_keys:
                self.selected_edge_keys = {picked_edge}
                self.selected_point_names.clear()
                self.selected_point_name = None
            if self.selected_edge_keys or len(self.selected_point_names) > 1:
                menu = QMenu(self)
                menu.addAction(
                    "Edit selected corridor widths / doors",
                    self.manage_corridor_settings,
                )
                menu.addAction(
                    "Create people movement profile",
                    self.create_people_profile_from_selection,
                )
                menu.addAction(
                    "Add selection to scenario",
                    self.manage_scenario_testing,
                )
                classify_menu = menu.addMenu("Classify corridor use")
                for label, area in [
                    ("No restriction", "none"),
                    ("Staff", "staff"),
                    ("Public", "public"),
                    ("Mixed staff/public", "both"),
                ]:
                    action = classify_menu.addAction(label)
                    action.triggered.connect(
                        lambda _checked=False, value=area: self.classify_selected_corridors(value)
                    )
                if self.selected_corridor_node_names():
                    door_menu = menu.addMenu("Door nodes")
                    door_menu.addAction(
                        "Mark selected nodes as doors…",
                        self.mark_selected_nodes_as_doors,
                    )
                    door_menu.addAction(
                        "Remove door classification",
                        self.clear_selected_node_doors,
                    )
                menu.addSeparator()
                menu.addAction("Clear selection", self.clear_topology_selection)
                menu.exec(event.globalPosition().toPoint())
                self.refresh_canvas()
                return

        if mode == "location_bbox" and self.bounding_box_location_name:
            hit_index = self.find_nearest_bounding_box_point_index(x, y)

            if hit_index is not None:
                removed = self.bounding_box_points.pop(hit_index)
                self.dragging_bounding_box_point_index = None
                self.set_status(
                    f"Removed bounding box point {hit_index + 1} "
                    f"from {self.bounding_box_location_name}"
                )
                self.refresh_canvas()
                return
        if mode == "location_bbox":
            if not picked:
                if self.bounding_box_location_name:
                    self.finish_location_bounding_box_draw()
                return
            picked_point = self.store.all_points().get(picked, {})
            if picked_point.get("kind") == "location":
                self.selected_point_name = picked
                self.refresh_canvas()
                menu = QMenu(self)
                draw_action = menu.addAction("Draw / replace bounding box")
                edit_action = None
                remove_action = None
                if self.store.get_location(picked).get("bounding_box"):
                    edit_action = menu.addAction("Edit existing bounding box")
                    remove_action = menu.addAction("Remove bounding box")
                cancel_action = None
                if self.bounding_box_location_name:
                    cancel_action = menu.addAction(
                        "Cancel current bounding box drawing"
                    )
                action = menu.exec(event.globalPosition().toPoint())
                if action == draw_action:
                    self.start_location_bounding_box_draw(picked, keep_existing=False)
                elif edit_action is not None and action == edit_action:
                    self.start_location_bounding_box_draw(picked, keep_existing=True)
                elif remove_action is not None and action == remove_action:
                    self.store.remove_location_bounding_box(picked)
                    self.set_status(f"Removed bounding box for {picked}")
                    self.refresh_canvas()
                elif cancel_action is not None and action == cancel_action:
                    self.cancel_location_bounding_box_draw()
                return

        if mode == "edge":
            # Right click empty space cancels edge chaining
            if not picked:
                if self.selected_for_edge is not None:
                    self.selected_for_edge = None
                    self.edge_delete_start = None
                    self.set_status("Edge chaining cancelled")
                return
            if picked and self.edge_delete_start is None:
                picked_point = self.store.all_points().get(picked, {})
                if picked_point.get("kind") == "department":
                    self.set_status("Departments cannot be connected by corridor edges")
                    return
                self.edge_delete_start = picked
                self.selected_for_edge = None
                self.set_status(f"Edge delete start selected: {picked}")
                return

            if picked:
                picked_point = self.store.all_points().get(picked, {})
                if picked_point.get("kind") == "department":
                    self.set_status("Departments cannot be connected by corridor edges")
                    return

            if picked and self.edge_delete_start:
                removed = False
                before = len(self.store.data.get("corridors", {}).get("edges", []))
                self.store.remove_edge(self.edge_delete_start, picked)
                after = len(self.store.data.get("corridors", {}).get("edges", []))
                removed = removed or (after < before)
                if self.bidirectional_check.isChecked():
                    before = len(self.store.data.get("corridors", {}).get("edges", []))
                    self.store.remove_edge(picked, self.edge_delete_start)
                    after = len(self.store.data.get("corridors", {}).get("edges", []))
                    removed = removed or (after < before)
                self.edge_delete_start = None
                self.set_status(
                    "Edge removed" if removed else "No matching edge to remove"
                )
                self.refresh_canvas()
                return
        if mode == "select_move" and picked:
            self.selected_point_name = picked
            self.refresh_canvas()
            point = self.store.all_points().get(picked, {})
            menu = QMenu(self)

            if point.get("kind") == "department":
                edit_department_action = menu.addAction("Edit department")
                delete_department_action = menu.addAction("Delete department")
                action = menu.exec(event.globalPosition().toPoint())

                if action == edit_department_action:
                    self.edit_department_by_name(picked)
                elif action == delete_department_action:
                    dept = next(
                        (
                            x
                            for x in self.store.data.get("departments", [])
                            if x.get("name") == picked
                        ),
                        None,
                    )
                    if dept:
                        self.store.delete_department(dept.get("id", ""))
                        self.set_status(f"Deleted department {picked}")
                        self.refresh_canvas()
                return

            if point.get("kind") == "location":
                draw_bounds_action = menu.addAction("Add / edit room bounding box")
                remove_bounds_action = None
                location = self.store.get_location(picked)
                if location and location.get("bounding_box"):
                    remove_bounds_action = menu.addAction("Remove room bounding box")
                menu.addSeparator()
                inventory_spaces_action = menu.addAction("Inventory spaces")
                menu.addSeparator()
            else:
                draw_bounds_action = None
                remove_bounds_action = None

            show_edges_action = menu.addAction("Show all edge connections")
            create_department_action = menu.addAction("Create department here")
            action = menu.exec(event.globalPosition().toPoint())

            if draw_bounds_action is not None and action == draw_bounds_action:
                self.start_location_bounding_box_draw(
                    picked,
                    keep_existing=bool(
                        self.store.get_location(picked).get("bounding_box")
                    ),
                )
            elif remove_bounds_action is not None and action == remove_bounds_action:
                self.store.remove_location_bounding_box(picked)
                self.set_status(f"Removed bounding box for {picked}")
                self.refresh_canvas()
            elif point.get("kind") == "location" and action == inventory_spaces_action:
                dialog = InventorySpacesDialog(self, picked)
                if dialog.exec() == QDialog.Accepted:
                    self.set_status(f"Updated inventory spaces for {picked}")
                    self.refresh_canvas()
            elif action == show_edges_action:
                self._show_edge_connections_dialog(picked)
            elif action == create_department_action:
                point = self.store.all_points().get(picked)
                if point:
                    self.create_department_at_position(
                        float(point["x"]),
                        float(point["y"]),
                        int(point["floor"]),
                    )
            return
        if picked:
            self.selected_point_name = picked
            self.refresh_canvas()

    def on_drag(self, event, sx, sy):
        if self.route_profile_selection_active:
            if not (event.modifiers() & Qt.AltModifier):
                return

            scene_pos = self.canvas.mapToScene(event.position().toPoint())

            if self.route_profile_selection_rect_start is None:
                self.route_profile_selection_rect_start = scene_pos
                self.route_profile_selection_rect_item = self.scene.addRect(
                    QRectF(scene_pos, scene_pos),
                    QPen(QColor("#00e5ff"), 0),
                    QBrush(QColor(0, 229, 255, 35)),
                )
                self.route_profile_selection_rect_item.setZValue(200)
                return

            rect = QRectF(
                self.route_profile_selection_rect_start, scene_pos
            ).normalized()

            if self.route_profile_selection_rect_item is not None:
                self.route_profile_selection_rect_item.setRect(rect)

            return
        mode = self._current_edit_mode()
        if mode == "pan":
            current = event.position().toPoint()
            if self.last_pan is None:
                self.last_pan = current
                return
            dx = current.x() - self.last_pan.x()
            dy = current.y() - self.last_pan.y()
            self.canvas.horizontalScrollBar().setValue(
                self.canvas.horizontalScrollBar().value() - dx
            )
            self.canvas.verticalScrollBar().setValue(
                self.canvas.verticalScrollBar().value() - dy
            )
            self.last_pan = current
            self.canvas.viewport().update()
            return
        if (
            mode == "location_bbox"
            and self.dragging_bounding_box_point_index is not None
            and self.bounding_box_location_name
        ):
            x, y = self.scene_to_world(sx, sy)
            x, y = self.snap(x, y)

            idx = self.dragging_bounding_box_point_index
            if 0 <= idx < len(self.bounding_box_points):
                self.bounding_box_points[idx] = {"x": x, "y": y}
                self.refresh_canvas()
            return
        if mode == "select_move" and self.drag_mode_active and self.dragging_point_name:
            x, y = self.scene_to_world(sx, sy)
            x, y = self.snap(x, y)
            self.store.set_point_position(self.dragging_point_name, x, y)
            self.refresh_canvas()

    def on_middle_click(self, event):
        self.last_pan = event.position().toPoint()

    def on_middle_drag(self, event):
        current = event.position().toPoint()
        if self.last_pan is None:
            self.last_pan = current
            return
        dx = current.x() - self.last_pan.x()
        dy = current.y() - self.last_pan.y()
        self.canvas.horizontalScrollBar().setValue(
            self.canvas.horizontalScrollBar().value() - dx
        )
        self.canvas.verticalScrollBar().setValue(
            self.canvas.verticalScrollBar().value() - dy
        )
        self.last_pan = current
        self.canvas.viewport().update()

    def on_middle_release(self, event):
        self.last_pan = None
        self.refresh_canvas()

    def on_mousewheel(self, event):
        factor = 1.1 if event.angleDelta().y() > 0 else 0.9
        self.canvas.scale(factor, factor)
        self.canvas.viewport().update()

    def _show_config_validation_warning(self, path: str, errors: list[str]) -> None:
        preview_limit = 50
        shown_errors = errors[:preview_limit]
        message_lines = [
            f"{Path(path).name} was opened, but validation found {len(errors)} issue(s).",
            "",
            "You can continue editing the file and use Validate JSON after making changes.",
            "",
            *shown_errors,
        ]
        if len(errors) > preview_limit:
            message_lines.append(
                f"... plus {len(errors) - preview_limit} additional issue(s)."
            )
        QMessageBox.warning(
            self,
            "Config opened with validation issues",
            "\n".join(message_lines),
        )

    def load_json_file(self, path: str) -> bool:
        try:
            store = JsonStore.from_file(path)
        except json.JSONDecodeError as exc:
            QMessageBox.critical(
                self,
                "Could not open JSON",
                f"{Path(path).name} is not valid JSON.\n\n{exc}",
            )
            self.set_status("JSON open failed: invalid JSON syntax")
            return False
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not open JSON",
                f"{Path(path).name} could not be read.\n\n{exc}",
            )
            self.set_status("JSON open failed: file could not be read")
            return False
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not open JSON",
                f"{Path(path).name} could not be loaded.\n\n{exc}",
            )
            self.set_status("JSON open failed")
            return False

        if not isinstance(store.data, dict):
            QMessageBox.critical(
                self,
                "Could not open JSON",
                f"{Path(path).name} is valid JSON, but the top-level value is not an object.",
            )
            self.set_status("JSON open failed: top-level value is not an object")
            return False

        try:
            validation_errors = store.validate()
        except Exception as exc:
            validation_errors = [
                f"Validation could not complete. The file was opened for editing, but some editor features may need missing fields to be repaired first: {exc}"
            ]

        self.store = store
        self.current_json_path = path
        self.selected_point_name = None
        self.selected_point_names.clear()
        self.selected_edge_keys.clear()
        self.dragging_point_name = None
        self.drag_mode_active = False
        self._clear_dxf_cache()
        current_floor = self.floor_spin.value()
        self._pending_fit_after_load = bool(self.get_floor_dxf_path(current_floor))
        self._queue_all_floor_dxf_loads(active_floor=current_floor, force_reload=False)
        if validation_errors:
            self.set_status(
                f"Opened {Path(path).name} with {len(validation_errors)} validation issue(s)"
            )
            self._show_config_validation_warning(path, validation_errors)
        else:
            self.set_status(f"Opened {Path(path).name}")
        self.refresh_canvas()
        self.fit_view()
        return True

    def open_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open JSON", "", "JSON files (*.json)"
        )
        if not path:
            return
        self.load_json_file(path)

    def save_json(self):
        path = self.current_json_path
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save JSON", "", "JSON files (*.json)"
            )
        if not path:
            return
        self.store.save(path)
        self.current_json_path = path
        self.set_status(f"Saved {Path(path).name}")
        self.refresh_canvas()

    def manage_simulation_settings(self):
        dialog = SimulationSettingsDialog(
            self,
            self.store.data.setdefault("simulation", {}),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.store.data["simulation"] = dialog.result
            end_text = dialog.result.get("end_datetime", "") or "not set"
            self.set_status(f"Simulation end date set to {end_text}")

    def manage_scenario_testing(self):
        dialog = ScenarioTestingDialog(
            self,
            self.store.scenario_testing(),
            self.store.data,
            topology_selection=self.topology_selection_payload(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.store.set_scenario_testing(dialog.result)
            simulation = self.store.data.setdefault("simulation", {})
            simulation["scenario_mode"] = bool(dialog.result.get("enabled", False))
            simulation["scenario_enhanced_logging"] = bool(dialog.result.get("enhanced_logging", False))
            self.set_status(
                f"Scenario testing set to {dialog.result.get('active_scenario', 'Normal operation')}"
            )

    def manage_people_movement(self):
        locations = sorted(self.store.all_points().keys())
        corridor_options = sorted(
            {
                f"{edge.get('from', '')} -> {edge.get('to', '')}"
                for edge in self.store.data.get("corridors", {}).get("edges", [])
                if edge.get("from") and edge.get("to")
            }
        )
        dialog = PeopleMovementListDialog(
            self,
            locations,
            corridor_options,
            self.store.people_movements(),
            initially_selected_corridors=self.selected_corridor_labels(
                include_incident_nodes=True
            ),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.store.set_people_movements(dialog.result)
            profile_by_edge = {}
            valid_profile_ids = {
                str(profile.get("id", "") or "").strip()
                for profile in dialog.result
                if str(profile.get("id", "") or "").strip()
            }
            direct_profile_ids = {
                str(profile.get("id", "") or "").strip()
                for profile in dialog.result
                if str(profile.get("id", "") or "").strip()
                and bool(profile.get("corridor_edges", []) or [])
            }
            for profile in dialog.result:
                if not bool(profile.get("enabled", True)):
                    continue
                group_type = str(profile.get("group_type", "staff") or "staff").strip().lower()
                if group_type == "mixed":
                    group_type = "both"
                profile_id = str(profile.get("id", "") or "").strip()
                for label in profile.get("corridor_edges", []) or []:
                    if " -> " not in str(label):
                        continue
                    start, end = str(label).split(" -> ", 1)
                    key = self._physical_edge_key(start, end)
                    entry = profile_by_edge.setdefault(key, {"types": set(), "profiles": set()})
                    entry["types"].add(group_type)
                    if profile_id:
                        entry["profiles"].add(profile_id)
            for edge in self.store.data.get("corridors", {}).get("edges", []):
                key = self._physical_edge_key(edge.get("from"), edge.get("to"))
                entry = profile_by_edge.get(key)
                retained_profiles = {
                    str(profile_id).strip()
                    for profile_id in edge.get("people_profile_ids", []) or []
                    if str(profile_id).strip() in valid_profile_ids
                    and str(profile_id).strip() not in direct_profile_ids
                }
                if entry is not None:
                    types = entry["types"]
                    edge["people_area_type"] = (
                        "both" if "both" in types or len(types) > 1 else next(iter(types), "none")
                    )
                    retained_profiles.update(entry["profiles"])
                edge["people_profile_ids"] = sorted(retained_profiles)
            self.store.ensure_corridor_defaults()
            self.set_status(f"Updated {len(dialog.result)} people movement profile(s)")
            self.refresh_canvas()

    def manage_corridor_settings(self):
        building = self.store.data.setdefault("building", {})
        corridors = self.store.data.setdefault("corridors", {})
        dialog = CorridorSettingsDialog(
            self,
            corridors.setdefault("edges", []),
            corridors.setdefault("nodes", []),
            default_width=float(
                building.get("default_corridor_width_m", 2.4) or 2.4
            ),
            default_door_width=float(
                building.get("default_door_clear_width_m", 0.9) or 0.9
            ),
            people_profiles=[
                item.get("id", "") for item in self.store.people_movements()
            ],
            initially_selected_edges=[
                f"{edge.get('from', '')} -> {edge.get('to', '')}"
                for edge in corridors.setdefault("edges", [])
                if self._physical_edge_key(edge.get("from"), edge.get("to"))
                in self._selected_corridor_keys(include_incident_nodes=False)
            ],
            initially_selected_nodes=self.selected_corridor_node_names(),
        )
        if dialog.exec() == QDialog.Accepted and dialog.result is not None:
            self.store.data["corridors"]["edges"] = dialog.result.get("edges", [])
            self.store.data["corridors"]["nodes"] = dialog.result.get("nodes", [])
            self.store.ensure_corridor_defaults()
            self.set_status(
                "Corridor widths, door openings, lane behaviour and people occupancy updated"
            )
            self.refresh_canvas()

    def load_dxf(self):
        floor = self.floor_spin.value()
        initialdir = ""
        existing = self.get_floor_dxf_path(floor)
        if existing:
            try:
                initialdir = str(Path(existing).expanduser().resolve().parent)
            except Exception:
                initialdir = str(Path(existing).expanduser().parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DXF", initialdir, "DXF files (*.dxf)"
        )
        if not path:
            return
        self.set_floor_dxf_path(floor, path)
        self._dxf_cache.pop(int(floor), None)
        if self.loaded_dxf_floor == int(floor):
            self.dxf_scene.clear()
            self.current_dxf_path = None
            self.loaded_dxf_floor = None
        self._pending_fit_after_load = True
        self._queue_all_floor_dxf_loads(active_floor=floor, force_reload=True)
        self.refresh_canvas()
        self.set_status(f"Mapped DXF {Path(path).name} to floor {floor}")

    def clear_floor_dxf(self):
        floor = self.floor_spin.value()
        existing = self.get_floor_dxf_path(floor)
        if not existing:
            self.set_status(f"No DXF mapped to floor {floor}")
            return
        if (
            QMessageBox.question(
                self, "Clear floor DXF", f"Remove DXF mapping for floor {floor}?"
            )
            != QMessageBox.Yes
        ):
            return
        self.clear_floor_dxf_mapping(floor)
        self._dxf_cache.pop(int(floor), None)
        self._dxf_loading_floors.discard(int(floor))
        if self.loaded_dxf_floor == int(floor):
            self.dxf_scene.clear()
            self.current_dxf_path = None
            self.loaded_dxf_floor = None
        self.set_status(f"Removed DXF mapping from floor {floor}")
        self.refresh_canvas()

    def validate_json(self):
        errors = self.store.validate()
        if errors:
            QMessageBox.critical(self, "Validation errors", "\n".join(errors[:100]))
            self.set_status(f"Validation failed with {len(errors)} error(s)")
        else:
            QMessageBox.information(
                self, "Validation", "JSON structure is internally consistent."
            )
            self.set_status("Validation passed")

    def manage_payloads(self):
        location_names = sorted(
            str(x.get("name", "")).strip()
            for x in self.store.data.get("locations", [])
            if str(x.get("name", "")).strip()
        )

        dialog = PayloadListDialog(
            self,
            self.store.data.get("payloads", []),
            self._save_payloads,
            location_names=location_names,
        )
        dialog.exec()

    def _save_payloads(self, items):
        self.store.data["payloads"] = items
        self.set_status("Payloads updated")

    def manage_lifts(self):
        dialog = LiftListDialog(self, self.store, self._lifts_changed)
        dialog.exec()

    def _lifts_changed(self):
        self.set_status("Lifts updated")
        self.refresh_canvas()

    def _save_lift_result(self, result, old_id=None):
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

    def manage_amrs(self):
        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))

        dialog = AMRListDialog(
            self,
            self.store.data.get("amrs", []),
            location_names,
            self._save_amrs,
        )
        dialog.exec()

    def _save_amrs(self, items):
        self.store.data["amrs"] = items
        if hasattr(self.store, "ensure_amr_defaults"):
            self.store.ensure_amr_defaults()
        self.set_status("AMRs updated")

    def build_floor_map(self, store):
        floor_map = {}
        for item in store.get("locations", []):
            floor_map[item["name"]] = int(item["floor"])
        for item in store.get("corridors", {}).get("nodes", []):
            floor_map[item["name"]] = int(item["floor"])
        for item in store.get("departments", []):
            name = str(item.get("name", "")).strip()
            if name:
                floor_map[name] = int(item.get("floor", 0))
        for lift in store.get("lifts", []):
            for floor_str in lift.get("floor_locations", {}).keys():
                floor_map[f"{lift['id']}-F{floor_str}"] = int(floor_str)
        return floor_map

    def _manual_task_amr_available(self):
        if not hasattr(self.store, "has_manual_task_compatible_amr"):
            return True
        return self.store.has_manual_task_compatible_amr()

    def manage_tasks(self):
        locations = self.store.data.get("locations", [])
        location_names = sorted(x["name"] for x in locations)
        payload_names = sorted(x["name"] for x in self.store.data.get("payloads", []))
        profile_names = [""] + sorted(self.store.data.get("route_profiles", {}).keys())
        floor_map = self.build_floor_map(self.store.data)
        TaskEditorWindow(
            self,
            self.store.data.get("tasks", []),
            location_names,
            payload_names,
            profile_names,
            self.store.suggest_next_task_id,
            self._save_tasks,
            floor_map=floor_map,
            manual_single_payload_available=self._manual_task_amr_available(),
        )

    def _save_tasks(self, items):
        self.store.data["tasks"] = items
        self.set_status("Tasks updated")

    def manage_task_planner(self):
        locations = self.store.data.get("locations", [])
        location_names = sorted(x["name"] for x in locations)
        payload_names = sorted(x["name"] for x in self.store.data.get("payloads", []))
        profile_names = [""] + sorted(self.store.data.get("route_profiles", {}).keys())
        floor_map = self.build_floor_map(self.store.data)
        for item in locations:
            floor_map[item["name"]] = int(item["floor"])
        TaskPlannerDialog(
            self,
            self.store.data.get("tasks", []),
            location_names,
            payload_names,
            profile_names,
            self.store.suggest_next_task_id,
            self._save_tasks,
            floor_map=floor_map,
            manual_single_payload_available=self._manual_task_amr_available(),
        )

    def manage_task_generation(self):
        locations = self.store.data.get("locations", [])
        location_names = sorted(x["name"] for x in locations)
        payload_names = sorted(x["name"] for x in self.store.data.get("payloads", []))
        profile_names = sorted(self.store.data.get("route_profiles", {}).keys())

        if hasattr(self.store, "task_generation"):
            task_generation = self.store.task_generation()
        else:
            task_generation = self.store.data.setdefault("task_generation", {})

        dialog = TaskGenerationSettingsDialog(
            self,
            task_generation,
            location_names,
            payload_names,
            profile_names,
            self.store.data.get("departments", []),
            self._save_task_generation,
        )
        dialog.exec()

    def _save_task_generation(self, config):
        if hasattr(self.store, "set_task_generation"):
            self.store.set_task_generation(config)
        else:
            self.store.data["task_generation"] = config
        self.set_status("Task generation parameters updated")

    def manage_route_profiles(self):
        point_names = set(self.store.names_in_use()) | {
            x["name"] for x in self.store.data.get("locations", [])
        }
        lift_ids = {x["id"] for x in self.store.data.get("lifts", [])}
        floor_map = self.build_floor_map(self.store.data)
        dialog = RouteProfilesEditorV2(
            self,
            self.store.data.get("route_profiles", {}),
            point_names,
            lift_ids,
            self.store.data.get("corridors", {}).get("edges", []),
            self._save_route_profiles,
            floor_map=floor_map,
        )
        dialog.exec()

    def _save_route_profiles(self, profiles):
        self.store.data["route_profiles"] = profiles
        self.set_status("Route profiles updated")

    def closeEvent(self, event):
        try:
            self._dxf_thread.quit()
            self._dxf_thread.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)

    def manage_mass_collections(self):
        dialog = MassCollectionListDialog(
            self,
            [loc["name"] for loc in self.store.data.get("locations", [])],
            [payload["name"] for payload in self.store.data.get("payloads", [])],
            self.store.data.setdefault("mass_collections", []),
            lambda items: self.store.data.__setitem__("mass_collections", items),
        )
        dialog.exec()

    def manage_waste_streams(self):
        payload_names = sorted(x["name"] for x in self.store.data.get("payloads", []))
        items = list(self.store.data.get("waste_streams", []))
        dialog = WasteStreamListDialog(
            self, payload_names, items, self._save_waste_streams
        )
        dialog.exec()

    def _save_waste_streams(self, items):
        self.store.data["waste_streams"] = items
        self.set_status("Waste streams updated")

    def manage_departments(self):
        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))
        waste_stream_names = sorted(
            x["name"] for x in self.store.data.get("waste_streams", [])
        )
        dialog = DepartmentListDialog(
            self,
            self.store.data.get("departments", []),
            location_names,
            waste_stream_names,
            current_floor=self.floor_spin.value(),
            on_save=self._save_departments,
            suggest_department_id=self.store.suggest_next_department_id,
            group_resolver=lambda item: f"Floor {self.build_floor_map(self.store.data).get(item, 'Other')}",
            task_generation_categories=self.task_generation_category_pairs(),
        )
        dialog.exec()

    def _save_departments(self, items):
        self.store.data["departments"] = items
        self.set_status("Departments updated")
        self.refresh_canvas()

    def manage_charging_locations(self):
        location_names = sorted(x["name"] for x in self.store.data.get("locations", []))

        picker = MultiSelectPicker(
            self,
            "Charging Locations",
            location_names,
            selected=self.store.charge_locations(),
            group_resolver=lambda item: f"Floor {self.build_floor_map(self.store.data).get(item, 'Other')}",
        )

        if picker.exec() == QDialog.Accepted and picker.result is not None:
            self.store.set_charge_locations(sorted(picker.result))
            self.set_status(f"Updated {len(picker.result)} charging location(s)")

    def start_route_profile_graphical_selection(
        self,
        allowed_point_names,
        selected_nodes,
        callback,
        return_window=None,
    ):
        self.route_profile_selection_active = True
        self.route_profile_allowed_point_names = set(allowed_point_names or [])
        self.route_profile_selected_nodes = set(selected_nodes or [])
        self.route_profile_selection_callback = callback
        self.route_profile_return_window = return_window
        self.route_profile_selection_rect_start = None
        self.route_profile_selection_rect_item = None

        self._set_edit_mode("select_move")
        self.set_status(
            "Route profile graphical selection: click nodes, Ctrl-click to add/remove, "
            "Alt-drag for rectangle selection, Ctrl+Alt-drag to add to selection, "
            "right-click empty space to finish."
        )
        self.refresh_canvas()

    def _route_profile_pickable_point_at(self, x, y, floor):
        picked = self.find_nearest_point_name(x, y, floor, radius_world=1.0)
        if not picked:
            return None

        if picked not in self.route_profile_allowed_point_names:
            return None

        return picked

    def _finish_route_profile_graphical_selection(self):
        selected = set(self.route_profile_selected_nodes)

        callback = self.route_profile_selection_callback
        return_window = self.route_profile_return_window

        self.route_profile_selection_active = False
        self.route_profile_allowed_point_names = set()
        self.route_profile_selected_nodes = set()
        self.route_profile_selection_callback = None
        self.route_profile_return_window = None
        self.route_profile_selection_rect_start = None

        if self.route_profile_selection_rect_item is not None:
            try:
                self.scene.removeItem(self.route_profile_selection_rect_item)
            except Exception:
                pass
        self.route_profile_selection_rect_item = None

        self.set_status(f"Route profile selection applied: {len(selected)} node(s)")
        self.refresh_canvas()

        if callback:
            callback(selected)

        if return_window:
            return_window.show()
            return_window.raise_()
            return_window.activateWindow()

    def _cancel_route_profile_graphical_selection(self):
        return_window = self.route_profile_return_window

        self.route_profile_selection_active = False
        self.route_profile_allowed_point_names = set()
        self.route_profile_selected_nodes = set()
        self.route_profile_selection_callback = None
        self.route_profile_return_window = None
        self.route_profile_selection_rect_start = None

        if self.route_profile_selection_rect_item is not None:
            try:
                self.scene.removeItem(self.route_profile_selection_rect_item)
            except Exception:
                pass
        self.route_profile_selection_rect_item = None

        self.set_status("Route profile graphical selection cancelled")
        self.refresh_canvas()

        if return_window:
            return_window.show()
            return_window.raise_()
            return_window.activateWindow()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    install_application_theme(app)
    window = AMRGraphEditor()
    window.show()
    return app.exec()
