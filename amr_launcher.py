"""PySide6 launcher for the AMR simulator toolchain.

The launcher deliberately leaves the existing editor, simulator, visualiser and
report tools unchanged.  It creates its own config and run folders, then starts
the existing scripts with explicit input/output paths.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "AMR Simulator"
REPO_ROOT = Path(__file__).resolve().parent
APP_ICON_PATH = REPO_ROOT / "assets" / "amr_simulator_icon.svg"
DEFAULT_WORKSPACE_ROOT = Path(r"D:\John\Documents\AMR Simulation")
SETTINGS_PATH = Path.home() / ".amr_simulator_launcher.json"
WORKSPACE_ROOT = DEFAULT_WORKSPACE_ROOT
LAUNCHER_CONFIG_DIR = WORKSPACE_ROOT / "launcher_configs"
LAUNCHER_CONFIG_ARCHIVE_DIR = LAUNCHER_CONFIG_DIR / "archive"
LAUNCHER_RUNS_DIR = WORKSPACE_ROOT / "launcher_runs"
MANIFEST_NAME = "run_manifest.json"
CONFIG_MANIFEST_SUFFIX = ".launcher_manifest.json"
CONFIG_MANIFEST_VERSION = 3
CONFIG_SUMMARY_FIELDS = {
    "locations",
    "departments",
    "floor_layouts",
    "floors",
    "amr_fleet",
    "amr_types",
    "payloads",
    "tasks",
    "route_profiles",
    "graph_nodes",
    "graph_edges",
}


def load_launcher_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_launcher_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def set_workspace_root(path: Path) -> None:
    global WORKSPACE_ROOT, LAUNCHER_CONFIG_DIR, LAUNCHER_CONFIG_ARCHIVE_DIR, LAUNCHER_RUNS_DIR
    WORKSPACE_ROOT = Path(path).expanduser().resolve()
    LAUNCHER_CONFIG_DIR = WORKSPACE_ROOT / "launcher_configs"
    LAUNCHER_CONFIG_ARCHIVE_DIR = LAUNCHER_CONFIG_DIR / "archive"
    LAUNCHER_RUNS_DIR = WORKSPACE_ROOT / "launcher_runs"


def load_workspace_root() -> None:
    settings = load_launcher_settings()
    configured_root = settings.get("workspace_root")
    set_workspace_root(Path(configured_root) if configured_root else DEFAULT_WORKSPACE_ROOT)


def ensure_dirs() -> None:
    LAUNCHER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHER_CONFIG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHER_RUNS_DIR.mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def display_datetime(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.replace("T", " ")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%d-%m-%Y %H:%M:%S")


def open_path(path: Path) -> None:
    path = path.resolve()
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        QDesktopServices.openUrl(path.as_uri())


def json_name(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        try:
            return path.relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            return str(path)


def discover_configs() -> List[Path]:
    """Return only configs curated by the launcher.

    External JSON files are intentionally ignored until a user imports or copies
    them into LAUNCHER_CONFIG_DIR.
    """
    candidates: Dict[str, Path] = {}
    search_roots = [LAUNCHER_CONFIG_DIR]
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            if path.name == MANIFEST_NAME:
                continue
            if path.name.endswith(CONFIG_MANIFEST_SUFFIX):
                continue
            candidates[str(path.resolve()).lower()] = path.resolve()
    return sorted(candidates.values(), key=lambda item: item.name.lower())


def discover_runs() -> List[Path]:
    if not LAUNCHER_RUNS_DIR.exists():
        return []
    runs = sorted({path.parent for path in LAUNCHER_RUNS_DIR.rglob(MANIFEST_NAME)})
    return sorted(runs, key=lambda item: item.name.lower(), reverse=True)


def load_manifest(run_dir: Path) -> dict:
    path = run_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_manifest(run_dir: Path, data: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / MANIFEST_NAME).write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def config_manifest_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}{CONFIG_MANIFEST_SUFFIX}")


def config_modified_at(config_path: Path) -> str:
    return datetime.fromtimestamp(config_path.stat().st_mtime).isoformat(
        timespec="seconds"
    )


def load_config_manifest(config_path: Path) -> dict:
    path = config_manifest_path(config_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_count(data: dict, key: str) -> int:
    value = data.get(key, [])
    return len(value) if isinstance(value, (dict, list)) else 0


def count_config_floors(data: dict) -> int:
    floors = set()
    floor_layouts = data.get("floor_dxf_files", [])
    if isinstance(floor_layouts, list):
        floors.update(item.get("floor") for item in floor_layouts if isinstance(item, dict))
    for section_name in ("locations",):
        section = data.get(section_name, [])
        if isinstance(section, list):
            floors.update(item.get("floor") for item in section if isinstance(item, dict))
    corridors = data.get("corridors", {})
    if isinstance(corridors, dict):
        nodes = corridors.get("nodes", [])
        if isinstance(nodes, list):
            floors.update(item.get("floor") for item in nodes if isinstance(item, dict))
    return len({floor for floor in floors if floor is not None})


def summarize_amrs(data: dict) -> tuple[int, int]:
    amrs = data.get("amrs", [])
    if not isinstance(amrs, list):
        return 0, 0
    total_devices = 0
    type_names = set()
    for index, amr in enumerate(amrs):
        if not isinstance(amr, dict):
            total_devices += 1
            type_names.add(str(index))
            continue
        type_names.add(str(amr.get("id") or amr.get("name") or index))
        try:
            total_devices += int(amr.get("quantity", 1))
        except (TypeError, ValueError):
            total_devices += 1
    return total_devices, len(type_names)


def build_config_manifest(config_path: Path) -> dict:
    modified_at = config_modified_at(config_path)
    manifest = {
        "manifest_version": CONFIG_MANIFEST_VERSION,
        "config_file": config_path.name,
        "config_path": str(config_path),
        "config_modified_at": modified_at,
        "summary_generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "locations": 0,
            "departments": 0,
            "floor_layouts": 0,
            "floors": 0,
            "amr_fleet": 0,
            "amr_types": 0,
            "payloads": 0,
            "tasks": 0,
            "route_profiles": 0,
            "graph_nodes": 0,
            "graph_edges": 0,
        },
        "checks": {"status": "ok", "warnings": []},
    }
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        manifest["checks"] = {
            "status": "error",
            "warnings": [f"Could not read config JSON: {exc}"],
        }
        return manifest

    corridors = data.get("corridors", {})
    if not isinstance(corridors, dict):
        corridors = {}
        manifest["checks"]["warnings"].append("Missing or invalid corridors section")

    amr_fleet, amr_types = summarize_amrs(data)
    manifest["summary"] = {
        "locations": list_count(data, "locations"),
        "departments": list_count(data, "departments"),
        "floor_layouts": list_count(data, "floor_dxf_files"),
        "floors": count_config_floors(data),
        "amr_fleet": amr_fleet,
        "amr_types": amr_types,
        "payloads": list_count(data, "payloads"),
        "tasks": list_count(data, "tasks"),
        "route_profiles": list_count(data, "route_profiles"),
        "graph_nodes": len(corridors.get("nodes", []))
        if isinstance(corridors.get("nodes", []), list)
        else 0,
        "graph_edges": len(corridors.get("edges", []))
        if isinstance(corridors.get("edges", []), list)
        else 0,
    }
    expected_sections = ["locations", "amrs", "payloads", "tasks", "corridors"]
    missing = [key for key in expected_sections if key not in data]
    if missing:
        manifest["checks"]["warnings"].append(
            f"Missing sections: {', '.join(missing)}"
        )
    if manifest["checks"]["warnings"]:
        manifest["checks"]["status"] = "warning"
    return manifest


def ensure_config_manifest(config_path: Path) -> dict:
    manifest = load_config_manifest(config_path)
    try:
        modified_at = config_modified_at(config_path)
    except OSError as exc:
        return {
            "config_file": config_path.name,
            "checks": {"status": "error", "warnings": [str(exc)]},
            "summary": {},
        }
    if manifest.get("config_modified_at") == modified_at and manifest.get("summary"):
        summary = manifest.get("summary", {})
        if (
            manifest.get("manifest_version") == CONFIG_MANIFEST_VERSION
            and CONFIG_SUMMARY_FIELDS.issubset(summary)
        ):
            return manifest
    manifest = build_config_manifest(config_path)
    try:
        config_manifest_path(config_path).write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        manifest["checks"] = {
            "status": "error",
            "warnings": [f"Could not write config summary: {exc}"],
        }
    return manifest


def unique_child_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    return folder / f"{stem}_{now_stamp()}{suffix}"


def safe_path_name(value: str, fallback: str = "untitled") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def unique_run_dir(folder: Path, folder_name: str) -> Path:
    target = folder / folder_name
    if not target.exists():
        return target
    suffix = 2
    while True:
        candidate = folder / f"{folder_name}_{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def is_launcher_config(path: Path) -> bool:
    try:
        path.resolve().relative_to(LAUNCHER_CONFIG_DIR.resolve())
    except ValueError:
        return False
    return path.resolve().parent == LAUNCHER_CONFIG_DIR.resolve()


class LauncherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(1100, 720)

        self.process: Optional[QProcess] = None
        self.current_run_dir: Optional[Path] = None
        self.active_process_kind = ""
        self.cancel_requested = False

        ensure_dirs()

        self.config_paths: List[Path] = []
        self.run_paths: List[Path] = []
        self.run_report_available: Dict[str, bool] = {}

        self._build_ui()
        self.refresh_all()

    def _build_ui(self) -> None:
        self._build_menu()

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.tabs = QTabWidget()
        left_layout.addWidget(self.tabs, 1)

        self.config_list = QListWidget()
        self.config_list.currentItemChanged.connect(self._on_config_selected)
        self.config_list.itemClicked.connect(lambda _item: self.show_selected_config_detail())
        self.tabs.addTab(self._config_tab(), "Configs")

        self.run_list = QListWidget()
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        self.run_list.itemClicked.connect(lambda _item: self.show_selected_run_detail())
        self.tabs.addTab(self._runs_tab(), "Runs")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.detail_label = QLabel("Select a config or run.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.detail_label.setFixedHeight(self.detail_label.fontMetrics().lineSpacing() * 9)
        self.detail_group = QGroupBox("Details")
        self.detail_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        detail_layout = QVBoxLayout(self.detail_group)
        detail_layout.addWidget(self.detail_label)
        right_layout.addWidget(self.detail_group)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        log_group = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_box)
        right_layout.addWidget(log_group, 1)

        button_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_all)
        self.cancel_btn = QPushButton("Cancel Running Process")
        self.cancel_btn.clicked.connect(self.cancel_running_process)
        self.cancel_btn.setEnabled(False)
        button_row.addWidget(self.refresh_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addStretch(1)
        right_layout.addLayout(button_row)

        self.setCentralWidget(root)

    def _build_menu(self) -> None:
        settings_menu = self.menuBar().addMenu("&Settings")

        storage_action = QAction("&Launcher Storage Location...", self)
        storage_action.triggered.connect(self.choose_launcher_storage_location)
        settings_menu.addAction(storage_action)

        open_storage_action = QAction("&Open Launcher Storage Folder", self)
        open_storage_action.triggered.connect(self.open_launcher_storage_folder)
        settings_menu.addAction(open_storage_action)

        help_menu = self.menuBar().addMenu("&Help")

        about_action = QAction("&About / Credits", self)
        about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(about_action)

        license_action = QAction("View &Licence", self)
        license_action.triggered.connect(self.show_license_dialog)
        help_menu.addAction(license_action)

    def choose_launcher_storage_location(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Launcher Storage Location",
            str(WORKSPACE_ROOT),
        )
        if not selected:
            return
        new_root = Path(selected)
        set_workspace_root(new_root)
        ensure_dirs()
        settings = load_launcher_settings()
        settings["workspace_root"] = str(WORKSPACE_ROOT)
        save_launcher_settings(settings)
        self.refresh_all()
        QMessageBox.information(
            self,
            "Launcher Storage Location",
            (
                "Launcher-managed configs and runs will now be stored under:\n\n"
                f"{WORKSPACE_ROOT}"
            ),
        )

    def open_launcher_storage_folder(self) -> None:
        ensure_dirs()
        open_path(WORKSPACE_ROOT)

    def show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            "About AMR Simulator",
            (
                "<h3>AMR Simulator</h3>"
                "<p>A suite of tools to simulate Autonomous Mobile Robots, providing access "
                "to the editor, simulator, report generation and visualiser tools from "
                "one place.</p>"
                "<p><b>Original project:</b> Autonomous Mobile Robot Simulator</p>"
                "<p><b>Licence:</b> GNU Affero General Public License v3.0 "
                "(AGPL-3.0). Use Help &gt; View Licence to read the full licence "
                "text included with this repository.</p>"
                "<p><b>Credits:</b> Original simulator project by the principal "
                "upstream GitHub author, bomtellis, and contributors.</p>"
                "<p>This project has been made possible thanks to the support "
                "and contribution of the Healthier Futures Programme at Mid "
                "Cheshire Hospitals NHS Foundation Trust.</p>"
            ),
        )

    def show_license_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("AMR Simulator Licence")
        dialog.resize(760, 560)

        layout = QVBoxLayout(dialog)
        heading = QLabel("GNU Affero General Public License v3.0")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(heading)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        license_path = REPO_ROOT / "LICENSE"
        try:
            text.setPlainText(license_path.read_text(encoding="utf-8"))
        except OSError as exc:
            text.setPlainText(f"Could not read licence file:\n{license_path}\n\n{exc}")
        layout.addWidget(text, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        dialog.exec()

    def _config_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self.config_list, 1)

        action_box = QGroupBox("Config Actions")
        actions = QGridLayout(action_box)
        open_editor = QPushButton("Open in Editor")
        open_editor.clicked.connect(self.open_selected_config_in_editor)
        create_config = QPushButton("Create Config Copy")
        create_config.clicked.connect(self.create_config_copy)
        import_config = QPushButton("Import Config")
        import_config.clicked.connect(self.import_config)
        archive_config = QPushButton("Archive Config")
        archive_config.clicked.connect(self.archive_selected_config)
        delete_config = QPushButton("Delete Config")
        delete_config.clicked.connect(self.delete_selected_config)
        open_folder = QPushButton("Open Config Folder")
        open_folder.clicked.connect(lambda: open_path(LAUNCHER_CONFIG_DIR))
        actions.addWidget(open_editor, 0, 0)
        actions.addWidget(create_config, 0, 1)
        actions.addWidget(import_config, 1, 0)
        actions.addWidget(open_folder, 1, 1)
        actions.addWidget(archive_config, 2, 0)
        actions.addWidget(delete_config, 2, 1)
        layout.addWidget(action_box)

        run_box = QGroupBox("Run Simulation")
        run_layout = QFormLayout(run_box)
        self.run_name_edit = QLineEdit()
        self.run_name_edit.setPlaceholderText("Optional run name")
        self.verbose_combo = QComboBox()
        self.verbose_combo.addItems(["Verbose CSV enabled", "Summary outputs only"])
        run_button = QPushButton("Run Selected Config")
        run_button.clicked.connect(self.run_selected_config)
        run_layout.addRow("Run name", self.run_name_edit)
        run_layout.addRow("Output detail", self.verbose_combo)
        run_layout.addRow(run_button)
        layout.addWidget(run_box)

        return page

    def _runs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self.run_list, 1)

        action_box = QGroupBox("Run Actions")
        actions = QGridLayout(action_box)
        self.report_action_btn = QPushButton("Generate Report")
        self._style_run_action_button(
            self.report_action_btn,
            self.style().standardIcon(QStyle.SP_FileDialogContentsView),
        )
        self.report_action_btn.clicked.connect(self.handle_selected_report_action)

        visualise = QPushButton("Visualise")
        self._style_run_action_button(
            visualise,
            self.style().standardIcon(QStyle.SP_ComputerIcon),
        )
        visualise.clicked.connect(self.open_visualiser)

        open_folder = QPushButton("Open Folder")
        self._style_run_action_button(
            open_folder,
            self.style().standardIcon(QStyle.SP_DirOpenIcon),
        )
        open_folder.clicked.connect(self.open_selected_run_folder)

        delete_run = QPushButton("Delete")
        trash_icon = self.style().standardIcon(
            getattr(QStyle, "SP_TrashIcon", QStyle.SP_DialogDiscardButton)
        )
        self._style_run_action_button(delete_run, trash_icon)
        delete_run.clicked.connect(self.delete_selected_run)

        actions.addWidget(self.report_action_btn, 0, 0, 1, 2)
        actions.addWidget(visualise, 1, 0, 1, 2)
        actions.addWidget(open_folder, 2, 0, 1, 2)
        actions.addWidget(delete_run, 3, 0, 1, 2)
        layout.addWidget(action_box)

        return page

    @staticmethod
    def _style_run_action_button(button: QPushButton, icon: QIcon) -> None:
        button.setIcon(icon)
        button.setIconSize(QSize(18, 18))
        button.setMinimumHeight(34)
        button.setStyleSheet(
            "QPushButton { text-align: left; padding-left: 14px; padding-right: 12px; }"
        )

    def prompt_scenario_intent(self) -> Optional[str]:
        dialog = QDialog(self)
        dialog.setWindowTitle("Scenario Intent")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Optionally record the purpose of this run. This will be saved "
                "with the run manifest."
            )
        )
        intent_edit = QPlainTextEdit()
        intent_edit.setPlaceholderText(
            "Example: Compare baseline AMR operation against increased morning theatre demand."
        )
        intent_edit.setMinimumHeight(90)
        layout.addWidget(intent_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Start Run")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None
        return intent_edit.toPlainText().strip()

    def refresh_all(self) -> None:
        selected_config = self.selected_config_path()
        selected_run = self.selected_run_dir()
        self.refresh_configs(selected_config)
        self.refresh_runs(selected_run)
        self.refresh_active_detail()

    def refresh_configs(self, selected: Optional[Path] = None) -> None:
        self.config_paths = discover_configs()
        self.config_list.clear()
        selected_text = str(selected.resolve()).lower() if selected else ""
        for path in self.config_paths:
            ensure_config_manifest(path)
            item = QListWidgetItem(json_name(path))
            item.setData(Qt.UserRole, str(path))
            self.config_list.addItem(item)
            if selected_text and str(path).lower() == selected_text:
                self.config_list.setCurrentItem(item)
        if self.config_list.count() and self.config_list.currentRow() < 0:
            self.config_list.setCurrentRow(0)
        elif not self.config_list.count():
            if self.tabs.currentIndex() == 0:
                self.detail_group.setTitle("Config Details")
                self.detail_label.setText(
                    "No launcher-managed configs found. Import a JSON config or create "
                    "a copy in the launcher config folder to get started."
                )

    def refresh_runs(self, selected: Optional[Path] = None) -> None:
        self.run_paths = discover_runs()
        self.run_report_available = {}
        self.run_list.clear()
        selected_text = str(selected.resolve()).lower() if selected else ""
        for path in self.run_paths:
            manifest = load_manifest(path)
            label = manifest.get("name") or path.name
            outputs = manifest.get("outputs", {})
            report_pdf = Path(outputs.get("report_pdf", path / "simulation_report.pdf"))
            self.run_report_available[str(path.resolve()).lower()] = report_pdf.exists()
            item = QListWidgetItem(str(label))
            item.setData(Qt.UserRole, str(path))
            self.run_list.addItem(item)
            if selected_text and str(path).lower() == selected_text:
                self.run_list.setCurrentItem(item)
        if self.run_list.count() and self.run_list.currentRow() < 0:
            self.run_list.setCurrentRow(0)
        self.update_report_action_button()

    def selected_config_path(self) -> Optional[Path]:
        item = self.config_list.currentItem() if hasattr(self, "config_list") else None
        if item is None:
            return None
        return Path(item.data(Qt.UserRole))

    def selected_run_dir(self) -> Optional[Path]:
        item = self.run_list.currentItem() if hasattr(self, "run_list") else None
        if item is None:
            return None
        return Path(item.data(Qt.UserRole))

    def _on_config_selected(self) -> None:
        if self.tabs.currentIndex() != 0:
            return
        self.show_selected_config_detail()

    def show_selected_config_detail(self) -> None:
        self.detail_group.setTitle("Config Details")
        path = self.selected_config_path()
        if not path:
            self.detail_label.setText(
                "No launcher-managed configs found. Import a JSON config or create "
                "a copy in the launcher config folder to get started."
            )
            return
        manifest = ensure_config_manifest(path)
        summary = manifest.get("summary", {})
        checks = manifest.get("checks", {})
        warnings = checks.get("warnings", [])
        check_status = str(checks.get("status", "unknown")).upper()
        if warnings:
            check_status = f"{check_status} ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})"
        lines = [
            f"Config: {path.name}",
            f"Locations: {summary.get('locations', 0):,}     Departments: {summary.get('departments', 0):,}",
            f"Floor Layouts: {summary.get('floor_layouts', 0):,}/{summary.get('floors', 0):,}",
            f"AMR Fleet: {summary.get('amr_fleet', 0):,}     AMR Types: {summary.get('amr_types', 0):,}",
            f"Payloads: {summary.get('payloads', 0):,}",
            f"Tasks: {summary.get('tasks', 0):,}",
            f"Route profiles: {summary.get('route_profiles', 0):,}",
            f"Graph: {summary.get('graph_nodes', 0):,} nodes, {summary.get('graph_edges', 0):,} edges",
            f"Checks: {check_status}",
        ]
        self.detail_label.setText("\n".join(lines))

    def _on_run_selected(self) -> None:
        if self.tabs.currentIndex() != 1:
            return
        self.show_selected_run_detail()
        self.update_report_action_button()

    def show_selected_run_detail(self) -> None:
        self.detail_group.setTitle("Run Details")
        run_dir = self.selected_run_dir()
        if not run_dir:
            self.detail_label.setText("No launcher-managed runs found.")
            return
        manifest = load_manifest(run_dir)
        lines = [f"Run: {run_dir.name}", f"Folder: {run_dir}"]
        if manifest.get("config_source"):
            lines.append(f"Config source: {manifest['config_source']}")
        if manifest.get("started_at"):
            lines.append(f"Started: {display_datetime(manifest['started_at'])}")
        if manifest.get("completed_at"):
            lines.append(f"Completed: {display_datetime(manifest['completed_at'])}")
        if manifest.get("status"):
            lines.append(f"Status: {manifest['status']}")
        if manifest.get("scenario_intent"):
            lines.append(f"Intent: {manifest['scenario_intent']}")
        if manifest.get("report_completed_at"):
            lines.append(f"Report completed: {display_datetime(manifest['report_completed_at'])}")
        if self.selected_run_has_report():
            report_state = "available"
        elif self.selected_run_is_complete():
            report_state = "not generated"
        else:
            report_state = "only available for completed runs"
        lines.append(f"Report: {report_state}")
        self.detail_label.setText("\n".join(lines))

    def selected_run_is_complete(self) -> bool:
        run_dir = self.selected_run_dir()
        if not run_dir:
            return False
        manifest = load_manifest(run_dir)
        return str(manifest.get("status", "")).lower() == "complete"

    def selected_run_has_report(self) -> bool:
        run_dir = self.selected_run_dir()
        if not run_dir:
            return False
        key = str(run_dir.resolve()).lower()
        return bool(self.run_report_available.get(key, False))

    def update_report_action_button(self) -> None:
        if not hasattr(self, "report_action_btn"):
            return
        run_dir = self.selected_run_dir()
        if not run_dir:
            self.report_action_btn.setText("Generate Report")
            self.report_action_btn.setIcon(
                self.style().standardIcon(QStyle.SP_FileDialogContentsView)
            )
            self.report_action_btn.setEnabled(False)
            return
        if not self.selected_run_is_complete():
            self.report_action_btn.setText("Report Unavailable")
            self.report_action_btn.setIcon(
                self.style().standardIcon(QStyle.SP_FileDialogContentsView)
            )
            self.report_action_btn.setEnabled(False)
            return
        if self.selected_run_has_report():
            self.report_action_btn.setText("Open Report")
            self.report_action_btn.setIcon(
                self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
            )
        else:
            self.report_action_btn.setText("Generate Report")
            self.report_action_btn.setIcon(
                self.style().standardIcon(QStyle.SP_FileDialogContentsView)
            )
        self.report_action_btn.setEnabled(True)

    def handle_selected_report_action(self) -> None:
        if self.selected_run_has_report():
            self.open_selected_report()
        else:
            self.generate_report_for_selected_run()

    def refresh_active_detail(self) -> None:
        if self.tabs.currentIndex() == 1:
            self.show_selected_run_detail()
        else:
            self.show_selected_config_detail()

    def _on_tab_changed(self, _index: int) -> None:
        self.refresh_active_detail()

    def append_log(self, text: str) -> None:
        if not text:
            return
        self.log_box.appendPlainText(text.rstrip())

    def replace_log_line(self, text: str) -> None:
        if not text:
            return
        cursor = self.log_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.removeSelectedText()
        cursor.insertText(text.rstrip())
        self.log_box.setTextCursor(cursor)

    def append_process_output(self, text: str) -> None:
        if not text:
            return
        normalised = text.replace("\r\n", "\n")
        parts = normalised.split("\r")
        if parts[0]:
            self.append_log(parts[0])
        for part in parts[1:]:
            if "\n" in part:
                lines = part.split("\n")
                self.replace_log_line(lines[0])
                for line in lines[1:]:
                    if line:
                        self.append_log(line)
            elif part:
                self.replace_log_line(part)

    def start_process(
        self,
        program: str,
        args: List[str],
        cwd: Path,
        finished_callback=None,
    ) -> None:
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Process running", "Wait for the current process to finish.")
            return

        self.log_box.clear()
        self.cancel_requested = False
        self.active_process_kind = ""
        self.append_log(f"> {program} {' '.join(args)}")
        self.process = QProcess(self)
        self.process.setProgram(program)
        self.process.setArguments(args)
        self.process.setWorkingDirectory(str(cwd))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process_output)
        self.process.finished.connect(
            lambda code, status: self._process_finished(code, status, finished_callback)
        )
        self.cancel_btn.setEnabled(True)
        self.process.start()

    def start_detached(self, program: str, args: List[str], cwd: Path) -> bool:
        self.append_log(f"> {program} {' '.join(args)}")
        return QProcess.startDetached(program, args, str(cwd))

    def _read_process_output(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            errors="replace"
        )
        self.append_process_output(data)

    def _process_finished(self, code: int, _status, callback) -> None:
        if self.cancel_requested:
            self.append_log(f"Process cancelled with exit code {code}.")
        else:
            self.append_log(f"Process finished with exit code {code}.")
        self.cancel_btn.setEnabled(False)
        if callback:
            callback(code, self.cancel_requested)
        self.process = None
        self.active_process_kind = ""
        self.cancel_requested = False
        QTimer.singleShot(0, self.refresh_all)

    def cancel_running_process(self) -> None:
        if self.process is None or self.process.state() == QProcess.NotRunning:
            return
        if QMessageBox.question(
            self,
            "Cancel process",
            "Cancel the running process? Partial output files will be left in the run folder.",
        ) != QMessageBox.Yes:
            return
        self.cancel_requested = True
        self.append_log("Cancellation requested.")
        self.process.terminate()
        QTimer.singleShot(5000, self._kill_process_if_running)

    def _kill_process_if_running(self) -> None:
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.append_log("Process did not stop after terminate; killing it.")
            self.process.kill()

    def open_selected_config_in_editor(self) -> None:
        path = self.selected_config_path()
        if not path:
            QMessageBox.warning(self, "No config", "Select a config first.")
            return
        started = self.start_detached(
            sys.executable,
            [
                str(REPO_ROOT / "visualiser" / "amr_editor_main.py"),
                "--config",
                str(path),
            ],
            REPO_ROOT / "visualiser",
        )
        if not started:
            QMessageBox.critical(self, "Editor failed", "Could not start the editor.")
            return
        self.append_log(f"Editor opened with config: {path}")

    def create_config_copy(self) -> None:
        source = self.selected_config_path()
        if not source:
            QMessageBox.warning(self, "No template", "Select a source config first.")
            return
        name, ok = QFileDialog.getSaveFileName(
            self,
            "Create Config Copy",
            str(LAUNCHER_CONFIG_DIR / source.name),
            "JSON files (*.json)",
        )
        if not ok or not name:
            return
        target = Path(name)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        if target.parent.resolve() != LAUNCHER_CONFIG_DIR.resolve():
            target = LAUNCHER_CONFIG_DIR / target.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target = unique_child_path(target.parent, target.name)
        shutil.copy2(source, target)
        self.refresh_configs(target)
        QMessageBox.information(self, "Config created", f"Created:\n{target}")

    def import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Config",
            str(WORKSPACE_ROOT),
            "JSON files (*.json)",
        )
        if not path:
            return
        source = Path(path)
        target = unique_child_path(LAUNCHER_CONFIG_DIR, source.name)
        shutil.copy2(source, target)
        self.refresh_configs(target)

    def archive_selected_config(self) -> None:
        path = self.selected_config_path()
        if not path:
            QMessageBox.warning(self, "No config", "Select a config first.")
            return
        if not is_launcher_config(path):
            QMessageBox.warning(
                self,
                "Cannot archive",
                "Only launcher-managed configs can be archived.",
            )
            return
        if QMessageBox.question(
            self,
            "Archive config",
            f"Archive this config?\n\n{path.name}\n\nIt will be hidden from the launcher list.",
        ) != QMessageBox.Yes:
            return
        LAUNCHER_CONFIG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        target = unique_child_path(LAUNCHER_CONFIG_ARCHIVE_DIR, path.name)
        shutil.move(str(path), str(target))
        sidecar = config_manifest_path(path)
        if sidecar.exists():
            shutil.move(
                str(sidecar),
                str(unique_child_path(LAUNCHER_CONFIG_ARCHIVE_DIR, sidecar.name)),
            )
        self.refresh_configs()
        self.detail_label.setText(f"Archived config:\n{target}")

    def delete_selected_config(self) -> None:
        path = self.selected_config_path()
        if not path:
            QMessageBox.warning(self, "No config", "Select a config first.")
            return
        if not is_launcher_config(path):
            QMessageBox.warning(
                self,
                "Cannot delete",
                "Only launcher-managed configs can be deleted.",
            )
            return
        if QMessageBox.warning(
            self,
            "Delete config",
            f"Permanently delete this launcher config?\n\n{path.name}\n\nThis does not delete the original file that was imported.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        path.unlink()
        sidecar = config_manifest_path(path)
        if sidecar.exists():
            sidecar.unlink()
        self.refresh_configs()
        self.detail_label.setText(f"Deleted launcher config:\n{path.name}")

    def run_selected_config(self) -> None:
        config = self.selected_config_path()
        if not config:
            QMessageBox.warning(self, "No config", "Select a config first.")
            return

        scenario_intent = self.prompt_scenario_intent()
        if scenario_intent is None:
            return

        config_name = safe_path_name(config.stem, "config")
        name_text = self.run_name_edit.text().strip()
        safe_name = safe_path_name(name_text, "")
        timestamp = now_stamp()
        folder_name = (
            f"{config_name}_{timestamp}_{safe_name}"
            if safe_name
            else f"{config_name}_{timestamp}"
        )
        config_run_dir = LAUNCHER_RUNS_DIR / config_name
        config_run_dir.mkdir(parents=True, exist_ok=True)
        run_dir = unique_run_dir(config_run_dir, folder_name)
        run_dir.mkdir(parents=True, exist_ok=False)

        config_copy = run_dir / "config.json"
        shutil.copy2(config, config_copy)

        verbose_enabled = self.verbose_combo.currentIndex() == 0
        manifest = {
            "name": folder_name,
            "config_name": config.stem,
            "config_group": config_name,
            "scenario_intent": scenario_intent,
            "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "config_source": str(config),
            "config_copy": str(config_copy),
            "outputs": {
                "steps_csv": str(run_dir / "simulation_steps.csv"),
                "visualiser_csv": str(run_dir / "visualiser_steps.csv"),
                "failed_tasks_csv": str(run_dir / "failed_tasks.csv"),
                "transport_matrix_csv": str(run_dir / "transport_matrix.csv"),
                "route_lengths_csv": str(run_dir / "route_lengths.csv"),
                "charger_estimate_csv": str(run_dir / "charger_estimate.csv"),
                "scenario_impact_csv": str(run_dir / "scenario_impact.csv"),
                "report_pdf": str(run_dir / "simulation_report.pdf"),
            },
        }
        write_manifest(run_dir, manifest)
        self.current_run_dir = run_dir

        args = [
            str(REPO_ROOT / "simulator.py"),
            "--config",
            str(config_copy),
            "--verbose-csv",
            manifest["outputs"]["steps_csv"],
            "--failed-tasks-csv",
            manifest["outputs"]["failed_tasks_csv"],
            "--transport-matrix-csv",
            manifest["outputs"]["transport_matrix_csv"],
            "--route-lengths-csv",
            manifest["outputs"]["route_lengths_csv"],
            "--charger-estimate-csv",
            manifest["outputs"]["charger_estimate_csv"],
            "--scenario-impact-csv",
            manifest["outputs"]["scenario_impact_csv"],
        ]
        if verbose_enabled:
            args.extend(
                [
                    "--verbose",
                    "--visualiser-csv",
                    manifest["outputs"]["visualiser_csv"],
                ]
            )

        def finished(code: int, was_cancelled: bool) -> None:
            data = load_manifest(run_dir)
            data["status"] = "cancelled" if was_cancelled else ("complete" if code == 0 else "failed")
            data["completed_at"] = datetime.now().isoformat(timespec="seconds")
            data["exit_code"] = code
            write_manifest(run_dir, data)

        self.tabs.setCurrentIndex(1)
        self.start_process(sys.executable, args, REPO_ROOT, finished)

    def open_selected_run_folder(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        open_path(run_dir)

    def delete_selected_run(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        if (
            self.process is not None
            and self.process.state() != QProcess.NotRunning
            and self.current_run_dir is not None
            and self.current_run_dir.resolve() == run_dir.resolve()
        ):
            QMessageBox.warning(
                self,
                "Run in progress",
                "This run is currently active. Cancel or wait for it to finish before deleting it.",
            )
            return
        if QMessageBox.warning(
            self,
            "Delete run",
            f"Permanently delete this run folder and all outputs?\n\n{run_dir}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            QMessageBox.critical(self, "Delete failed", f"Could not delete run:\n{exc}")
            return
        if self.current_run_dir is not None and self.current_run_dir.resolve() == run_dir.resolve():
            self.current_run_dir = None
        self.refresh_runs()
        self.refresh_active_detail()
        self.append_log(f"Deleted run: {run_dir}")

    def open_visualiser(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        manifest = load_manifest(run_dir)
        outputs = manifest.get("outputs", {})
        csv_path = Path(
            outputs.get("visualiser_csv")
            or outputs.get("steps_csv", run_dir / "simulation_steps.csv")
        )
        config_json = run_dir / "config.json"
        if not config_json.exists():
            QMessageBox.warning(self, "Missing config", f"Run config not found:\n{config_json}")
            return
        if not csv_path.exists():
            QMessageBox.warning(self, "Missing CSV", f"Simulation CSV not found:\n{csv_path}")
            return
        started = self.start_detached(
            sys.executable,
            [
                str(REPO_ROOT / "visualiser" / "amr_sim_visualiser_pyside6.py"),
                "--config",
                str(config_json),
                "--csv",
                str(csv_path),
            ],
            REPO_ROOT / "visualiser",
        )
        if not started:
            QMessageBox.critical(self, "Visualiser failed", "Could not start the visualiser.")
            return
        self.append_log(f"Visualiser opened with config: {config_json}")
        self.append_log(f"Visualiser opened with CSV: {csv_path}")

    def generate_report_for_selected_run(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        if not self.selected_run_is_complete():
            QMessageBox.warning(
                self,
                "Run incomplete",
                "Reports can only be generated for runs that completed successfully.",
            )
            self.update_report_action_button()
            return
        manifest = load_manifest(run_dir)
        outputs = manifest.get("outputs", {})
        csv_path = Path(outputs.get("steps_csv", run_dir / "simulation_steps.csv"))
        failed_csv = Path(outputs.get("failed_tasks_csv", run_dir / "failed_tasks.csv"))
        report_pdf = Path(outputs.get("report_pdf", run_dir / "simulation_report.pdf"))
        config_json = run_dir / "config.json"
        if not csv_path.exists():
            QMessageBox.warning(self, "Missing CSV", f"Simulation CSV not found:\n{csv_path}")
            return

        args = [
            str(REPO_ROOT / "report" / "amr_report_main.py"),
            str(csv_path),
            "--output",
            str(report_pdf),
            "--config-json",
            str(config_json),
        ]
        if failed_csv.exists():
            args.extend(["--failed-tasks-csv", str(failed_csv)])

        def finished(code: int, was_cancelled: bool) -> None:
            data = load_manifest(run_dir)
            data["report_status"] = "cancelled" if was_cancelled else ("complete" if code == 0 else "failed")
            data["report_completed_at"] = datetime.now().isoformat(timespec="seconds")
            write_manifest(run_dir, data)

        self.start_process(sys.executable, args, REPO_ROOT / "report", finished)

    def open_selected_report(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        manifest = load_manifest(run_dir)
        outputs = manifest.get("outputs", {})
        report_pdf = Path(outputs.get("report_pdf", run_dir / "simulation_report.pdf"))
        if not report_pdf.exists():
            QMessageBox.warning(self, "No report", f"Report not found:\n{report_pdf}")
            return
        open_path(report_pdf)


def main() -> int:
    load_workspace_root()
    app = QApplication.instance() or QApplication(sys.argv)
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = LauncherWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
