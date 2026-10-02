"""Shared, responsive user-interface styling for the AMR editor dialogs.

The helpers in this module deliberately avoid changing application data or signal
wiring.  They standardise presentation, accessibility and small-screen behaviour
for both legacy and newer dialogs.
"""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTabWidget,
    QTimeEdit,
    QTreeView,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)


_THEME_MARKER = "/* AMR_DIALOG_THEME_V2 */"


APPLICATION_STYLESHEET = r"""
/* AMR_DIALOG_THEME_V2 */
QDialog, QMainWindow {
    background: palette(window);
}
QDialog QLabel#dialogIntro, QMainWindow QLabel#dialogIntro {
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 6px;
    padding: 9px 11px;
}
QGroupBox {
    font-weight: 600;
    border: 1px solid palette(mid);
    border-radius: 7px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit, QDateTimeEdit,
QPlainTextEdit, QTextEdit {
    min-height: 28px;
    padding: 2px 6px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    background: palette(base);
    selection-background-color: palette(highlight);
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTimeEdit:focus, QDateTimeEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 2px solid palette(highlight);
}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {
    background: palette(alternate-base);
}
QPushButton, QToolButton {
    min-height: 29px;
    padding: 3px 10px;
    border: 1px solid palette(mid);
    border-radius: 5px;
    background: palette(button);
}
QPushButton:hover, QToolButton:hover {
    border-color: palette(highlight);
}
QPushButton[primary="true"] {
    font-weight: 600;
    border: 2px solid palette(highlight);
}
QPushButton[danger="true"] {
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid palette(mid);
    border-radius: 5px;
    top: -1px;
}
QTabBar::tab {
    min-height: 27px;
    padding: 5px 12px;
}
QTableView, QTreeView, QListView {
    border: 1px solid palette(mid);
    border-radius: 5px;
    background: palette(base);
    alternate-background-color: palette(alternate-base);
    selection-background-color: palette(highlight);
}
QHeaderView::section {
    padding: 5px 7px;
    border: 0;
    border-right: 1px solid palette(mid);
    border-bottom: 1px solid palette(mid);
    background: palette(button);
    font-weight: 600;
}
QScrollArea {
    border: 0;
    background: transparent;
}
QScrollBar:vertical { width: 14px; }
QScrollBar:horizontal { height: 14px; }
QCheckBox { spacing: 6px; }
QLabel[sectionHeading="true"] {
    font-weight: 700;
    padding-top: 5px;
}
"""


INTRODUCTIONS = {
    "Scheduled times": "Add the times at which this task should be released. Duplicate times are ignored and the saved list is kept in chronological order.",
    "Staff working hours by day": "Choose the days and working window for this staff group. Clear the custom-hours option to inherit the global staff pattern instead.",
    "Global staff configuration": "Set the shared staff travel assumptions and standard shift patterns used by staff-assisted task generation.",
    "Task generation parameters": "Configure how tasks are generated, routed and staffed. Related controls are grouped together and disabled when they do not apply to the selected mode.",
    "Lifts": "Add, edit or remove lifts. Double-click a row to edit it and review the served floors, speed and capacity before running the simulation.",
    "Payloads": "Manage the payload types that AMRs can carry, including dimensions, contents and permitted carrying orientations.",
    "AMRs": "Manage AMR types, fleet quantities, payload slots, battery settings and charging behaviour.",
    "Waste Stream": "Define a waste stream and its payload, generation assumptions and collection behaviour.",
    "Waste Streams": "Manage the waste streams available to departments and mass-collection schedules.",
    "Mass collection / bin rotation": "Configure a repeatable collection or bin-rotation event, including timing, locations and payload handling.",
    "Mass collections / bin rotations": "Manage scheduled mass collections and bin rotations used by waste task generation.",
    "Department waste stream generation": "Choose which waste streams this department produces and configure each stream's generation settings.",
    "Waste stream generation settings": "Set the generation mode, quantities, schedule and collection rules for this department and waste stream.",
    "Department": "Configure the department identity, location assignments, task categories, working patterns and waste streams.",
    "Manage waste streams for selected departments": "Apply or remove waste-stream settings across several departments in one operation. Review the summary before saving bulk changes.",
    "Task category shared-location wizard": "Assign a common source or destination to the selected task category across multiple departments.",
    "Auto shared bin groups from category locations": "Create shared waste-bin groups from existing department category locations. The preview shows which departments will be grouped together.",
    "Departments": "Manage departments, their graph locations and task-generation settings. Use search and multi-selection for bulk operations.",
    "Create inventory space array": "Create several evenly spaced inventory positions inside the selected location. Check the preview values before applying the array.",
    "Route Profiles": "Define which lifts, graph nodes and corridor edges a route profile may use. Use the graphical selector for complex routes.",
    "Task": "Create or edit one manual transport task. Select graph locations rather than typing names to avoid invalid references.",
    "Create One-to-Many Tasks": "Create one task per selected destination using the same pickup, payload, release time and route profile.",
    "Select target days": "Select individual dates, a whole displayed month, or hold Shift while clicking to select a continuous date range.",
    "Task Planner": "Plan and review tasks on the calendar. Use the filters and bulk tools to make changes without editing each task separately.",
    "Edit Multiple Tasks": "Apply the chosen values to all selected tasks. Leave optional fields unchanged when they should retain their existing values.",
    "Tasks": "Manage manual tasks and task-planning tools. Use the table filters to find tasks before editing or deleting them.",
    "Loading DXFs": "The drawing files are being loaded and prepared for display. Progress and any failed files are shown below.",
    "Corridors, doors and people use": "Apply corridor widths, door restrictions and people-use classifications to one or more selected topology assets.",
    "People movement profiles": "Manage reusable staff and public movement profiles and assign them to graph routes or selected corridors.",
    "Scenario testing": "Configure temporary failures and restrictions, then compare the scenario outcome with normal operation.",
    "Simulation settings": "Set the simulation period, performance options, logging detail and output locations.",
}


PREFIX_INTRODUCTIONS = (
    ("Configure multiple departments -", "Apply one task-generation configuration to the selected departments. Only departments with a valid category location are available."),
    ("Edge Connections -", "Review and edit the graph connections for this node. Distances and direction determine the routes available to AMRs and people."),
    ("Inventory Spaces -", "Manage AMR and payload positions within this location. Charger and access settings affect parking, charging and payload handling."),
    ("Select ", "Search the available items, then select one or more entries. Use the visible-selection controls after filtering large lists."),
)


class _DialogThemeFilter(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, (QDialog, QMainWindow)):
            QTimer.singleShot(0, lambda obj=watched: polish_dialog(obj))
        return False


_filter_instance: Optional[_DialogThemeFilter] = None


def install_application_theme(app: Optional[QApplication] = None) -> None:
    """Install the shared QSS and automatic dialog polisher once per process."""
    global _filter_instance
    app = app or QApplication.instance()
    if app is None:
        return
    existing = app.styleSheet() or ""
    if _THEME_MARKER not in existing:
        app.setStyleSheet((existing + "\n" + APPLICATION_STYLESHEET).strip())
    if _filter_instance is None:
        _filter_instance = _DialogThemeFilter(app)
        app.installEventFilter(_filter_instance)


def _intro_for_title(title: str) -> str:
    title = str(title or "").strip()
    if title in INTRODUCTIONS:
        return INTRODUCTIONS[title]
    for prefix, text in PREFIX_INTRODUCTIONS:
        if title.startswith(prefix):
            return text
    if title and title not in {"AMR Simulation Graph Editor"}:
        return "Review the information below, make the required changes and use the primary action to apply them. Fields with units show the unit beside the value."
    return ""


def _content_host(dialog: QWidget) -> QWidget:
    if isinstance(dialog, QMainWindow) and dialog.centralWidget() is not None:
        return dialog.centralWidget()
    return dialog


def _direct_intro_exists(dialog: QWidget) -> bool:
    host = _content_host(dialog)
    root = host.layout()
    for label in host.findChildren(QLabel):
        if label.objectName() == "dialogIntro":
            return True
    if isinstance(root, QVBoxLayout) and root.count():
        first = root.itemAt(0).widget()
        if isinstance(first, QLabel) and first.wordWrap() and len(first.text().strip()) >= 70:
            first.setObjectName("dialogIntro")
            first.setFrameShape(QFrame.Shape.StyledPanel)
            first.setContentsMargins(10, 8, 10, 8)
            return True
    return False


def _insert_intro(dialog: QWidget) -> None:
    if _direct_intro_exists(dialog):
        return
    text = _intro_for_title(dialog.windowTitle())
    if not text:
        return
    host = _content_host(dialog)
    root = host.layout()
    if not isinstance(root, QVBoxLayout):
        return
    intro = QLabel(text, host)
    intro.setObjectName("dialogIntro")
    intro.setWordWrap(True)
    intro.setFrameShape(QFrame.Shape.StyledPanel)
    intro.setContentsMargins(10, 8, 10, 8)
    root.insertWidget(0, intro)


def _friendly_name(label: str) -> str:
    text = re.sub(r"\s*\([^)]*\)\s*", " ", str(label or ""))
    text = text.replace("/", " or ").replace(":", "")
    return " ".join(text.split()).strip()


def _polish_forms(root: QWidget) -> None:
    layouts = root.findChildren(QLayout)
    if root.layout() is not None:
        layouts = [root.layout()] + layouts
    seen = set()
    for layout in layouts:
        if id(layout) in seen:
            continue
        seen.add(id(layout))
        layout.setSpacing(max(7, layout.spacing()))
        margins = layout.contentsMargins()
        if margins.left() < 8 and layout is root.layout():
            layout.setContentsMargins(12, 12, 12, 12)
        if isinstance(layout, QFormLayout):
            layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            layout.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            for row in range(layout.rowCount()):
                label_item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                field_item = layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                label_widget = label_item.widget() if label_item else None
                field_widget = field_item.widget() if field_item else None
                if not isinstance(label_widget, QLabel) or field_widget is None:
                    continue
                label_text = _friendly_name(label_widget.text())
                label_widget.setWordWrap(True)
                if not field_widget.accessibleName():
                    field_widget.setAccessibleName(label_text)
                if not field_widget.toolTip() and label_text:
                    field_widget.setToolTip(label_text)
                if isinstance(field_widget, QLineEdit) and not field_widget.isReadOnly():
                    if not field_widget.placeholderText() and label_text:
                        field_widget.setPlaceholderText(f"Enter {label_text.lower()}")


def _polish_inputs(root: QWidget) -> None:
    for edit in root.findChildren(QLineEdit):
        if not edit.isReadOnly():
            edit.setClearButtonEnabled(True)
        edit.setMinimumWidth(min(max(edit.minimumWidth(), 120), 280))
    for combo in root.findChildren(QComboBox):
        combo.setMaxVisibleItems(20)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(min(max(combo.minimumContentsLength(), 12), 28))
    for spin in root.findChildren(QSpinBox) + root.findChildren(QDoubleSpinBox):
        spin.setKeyboardTracking(False)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setAccelerated(True)
    for edit in root.findChildren(QDateTimeEdit):
        edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        if not isinstance(edit, QTimeEdit):
            edit.setCalendarPopup(True)
    for tabs in root.findChildren(QTabWidget):
        tabs.setDocumentMode(False)
        tabs.setMovable(False)
        tabs.setUsesScrollButtons(True)
    for group in root.findChildren(QGroupBox):
        group.setFlat(False)


def _polish_item_views(root: QWidget) -> None:
    views = root.findChildren(QTableView) + root.findChildren(QTreeView) + root.findChildren(QListView)
    seen = set()
    for view in views:
        if id(view) in seen:
            continue
        seen.add(id(view))
        view.setAlternatingRowColors(True)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    for table in root.findChildren(QTableWidget):
        table.verticalHeader().setDefaultSectionSize(max(26, table.verticalHeader().defaultSectionSize()))
        table.horizontalHeader().setMinimumSectionSize(60)
        table.horizontalHeader().setHighlightSections(False)
        if table.columnCount() > 0 and table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed:
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for tree in root.findChildren(QTreeWidget):
        tree.setUniformRowHeights(True)
        tree.header().setHighlightSections(False)
    for listing in root.findChildren(QListWidget):
        listing.setSpacing(max(1, listing.spacing()))


def _polish_buttons(root: QWidget) -> None:
    danger_words = ("delete", "remove", "clear all", "reset all")
    for button in root.findChildren(QPushButton):
        text = button.text().replace("&", "").strip().lower()
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if any(word in text for word in danger_words):
            button.setProperty("danger", True)
        if text in {"save", "ok", "apply", "create", "finish", "run simulation"}:
            button.setProperty("primary", True)
    title = str(root.windowTitle() or "").strip()
    for box in root.findChildren(QDialogButtonBox):
        ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            if title.startswith("Select "):
                ok_button.setText("Apply selection")
            elif title.startswith("Create "):
                ok_button.setText("Create")
            elif title.startswith(("Configure ", "Manage ")) or title in {
                "Edit Multiple Tasks",
                "Corridors, doors and people use",
            }:
                ok_button.setText("Apply")
            elif title not in {""}:
                ok_button.setText("Save")
        for standard in (
            QDialogButtonBox.StandardButton.Save,
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Apply,
            QDialogButtonBox.StandardButton.Open,
        ):
            button = box.button(standard)
            if button is not None:
                button.setProperty("primary", True)
        box.setCenterButtons(False)


def _move_layout_item(item, destination: QVBoxLayout) -> None:
    widget = item.widget()
    child_layout = item.layout()
    spacer = item.spacerItem()
    if widget is not None:
        destination.addWidget(widget)
    elif child_layout is not None:
        destination.addLayout(child_layout)
    elif spacer is not None:
        destination.addItem(spacer)


def _make_small_screen_scrollable(dialog: QWidget) -> None:
    """Wrap oversized vertical dialog content while keeping action buttons visible."""
    if dialog.property("amrScrollWrapped"):
        return
    host = _content_host(dialog)
    root = host.layout()
    if not isinstance(root, QVBoxLayout):
        return
    if any(isinstance(root.itemAt(i).widget(), QScrollArea) for i in range(root.count())):
        return
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    hint = dialog.sizeHint()
    if hint.height() <= int(available.height() * 0.82):
        return

    button_item_index = -1
    for index in range(root.count() - 1, -1, -1):
        widget = root.itemAt(index).widget()
        if isinstance(widget, QDialogButtonBox):
            button_item_index = index
            break

    content_count = button_item_index if button_item_index >= 0 else root.count()
    if content_count <= 0:
        return

    content = QWidget(host)
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(2, 2, 2, 2)
    content_layout.setSpacing(root.spacing())
    for _ in range(content_count):
        item = root.takeAt(0)
        if item is not None:
            _move_layout_item(item, content_layout)
    content_layout.addStretch(0)

    scroll = QScrollArea(host)
    scroll.setObjectName("dialogContentScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    root.insertWidget(0, scroll, 1)
    dialog.setProperty("amrScrollWrapped", True)


def _fit_to_screen(dialog: QWidget) -> None:
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    max_width = max(480, int(available.width() * 0.94))
    max_height = max(360, int(available.height() * 0.92))
    dialog.setMaximumSize(max_width, max_height)
    current = dialog.size()
    hint = dialog.sizeHint()
    width = min(max(current.width(), min(hint.width(), max_width)), max_width)
    height = min(max(current.height(), min(hint.height(), max_height)), max_height)
    dialog.resize(width, height)


def polish_dialog(dialog: QWidget) -> None:
    """Apply the shared theme and responsive behaviour to a completed dialog."""
    if not isinstance(dialog, (QDialog, QMainWindow)):
        return
    install_application_theme()
    if isinstance(dialog, QMainWindow) and dialog.windowTitle() == "AMR Simulation Graph Editor":
        dialog.setProperty("amrDialogPolished", True)
        return
    if not dialog.property("amrDialogPolished"):
        dialog.setProperty("amrDialogPolished", True)
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if isinstance(dialog, QDialog):
            dialog.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
            dialog.setSizeGripEnabled(True)
        _insert_intro(dialog)
        _polish_forms(dialog)
        _polish_inputs(dialog)
        _polish_item_views(dialog)
        _polish_buttons(dialog)
        dialog.style().unpolish(dialog)
        dialog.style().polish(dialog)
    _make_small_screen_scrollable(dialog)
    _fit_to_screen(dialog)
