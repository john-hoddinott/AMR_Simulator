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

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "AMR Simulator Launcher"
REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(r"D:\John\Documents\AMR Simulation")
LAUNCHER_CONFIG_DIR = WORKSPACE_ROOT / "launcher_configs"
LAUNCHER_CONFIG_ARCHIVE_DIR = LAUNCHER_CONFIG_DIR / "archive"
LAUNCHER_RUNS_DIR = WORKSPACE_ROOT / "launcher_runs"
MANIFEST_NAME = "run_manifest.json"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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
            candidates[str(path.resolve()).lower()] = path.resolve()
    return sorted(candidates.values(), key=lambda item: item.name.lower())


def discover_runs() -> List[Path]:
    if not LAUNCHER_RUNS_DIR.exists():
        return []
    runs = [
        path
        for path in LAUNCHER_RUNS_DIR.iterdir()
        if path.is_dir() and (path / MANIFEST_NAME).exists()
    ]
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


def unique_child_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    return folder / f"{stem}_{now_stamp()}{suffix}"


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
        self.resize(1100, 720)

        self.process: Optional[QProcess] = None
        self.current_run_dir: Optional[Path] = None
        self.active_process_kind = ""
        self.cancel_requested = False

        LAUNCHER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHER_CONFIG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHER_RUNS_DIR.mkdir(parents=True, exist_ok=True)

        self.config_paths: List[Path] = []
        self.run_paths: List[Path] = []

        self._build_ui()
        self.refresh_all()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        header = QLabel(APP_TITLE)
        header.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(header)

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
        self.tabs.addTab(self._config_tab(), "Configs")

        self.run_list = QListWidget()
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        self.tabs.addTab(self._runs_tab(), "Runs")

        self.detail_label = QLabel("Select a config or run.")
        self.detail_label.setWordWrap(True)
        right_layout.addWidget(self.detail_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        right_layout.addWidget(self.log_box, 1)

        button_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_all)
        self.open_workspace_btn = QPushButton("Open Launcher Folder")
        self.open_workspace_btn.clicked.connect(lambda: open_path(WORKSPACE_ROOT))
        self.cancel_btn = QPushButton("Cancel Running Process")
        self.cancel_btn.clicked.connect(self.cancel_running_process)
        self.cancel_btn.setEnabled(False)
        button_row.addWidget(self.refresh_btn)
        button_row.addWidget(self.open_workspace_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addStretch(1)
        right_layout.addLayout(button_row)

        self.setCentralWidget(root)

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
        open_folder = QPushButton("Open Run Folder")
        open_folder.clicked.connect(self.open_selected_run_folder)
        visualise = QPushButton("Open Visualiser")
        visualise.clicked.connect(self.open_visualiser)
        report = QPushButton("Generate Report")
        report.clicked.connect(self.generate_report_for_selected_run)
        open_report = QPushButton("Open Report")
        open_report.clicked.connect(self.open_selected_report)
        actions.addWidget(open_folder, 0, 0)
        actions.addWidget(visualise, 0, 1)
        actions.addWidget(report, 1, 0)
        actions.addWidget(open_report, 1, 1)
        layout.addWidget(action_box)

        return page

    def refresh_all(self) -> None:
        selected_config = self.selected_config_path()
        selected_run = self.selected_run_dir()
        self.refresh_configs(selected_config)
        self.refresh_runs(selected_run)

    def refresh_configs(self, selected: Optional[Path] = None) -> None:
        self.config_paths = discover_configs()
        self.config_list.clear()
        selected_text = str(selected.resolve()).lower() if selected else ""
        for path in self.config_paths:
            item = QListWidgetItem(json_name(path))
            item.setData(Qt.UserRole, str(path))
            self.config_list.addItem(item)
            if selected_text and str(path).lower() == selected_text:
                self.config_list.setCurrentItem(item)
        if self.config_list.count() and self.config_list.currentRow() < 0:
            self.config_list.setCurrentRow(0)
        elif not self.config_list.count():
            self.detail_label.setText(
                "No launcher-managed configs found. Import a JSON config or create "
                "a copy in the launcher config folder to get started."
            )

    def refresh_runs(self, selected: Optional[Path] = None) -> None:
        self.run_paths = discover_runs()
        self.run_list.clear()
        selected_text = str(selected.resolve()).lower() if selected else ""
        for path in self.run_paths:
            manifest = load_manifest(path)
            label = manifest.get("name") or path.name
            item = QListWidgetItem(str(label))
            item.setData(Qt.UserRole, str(path))
            self.run_list.addItem(item)
            if selected_text and str(path).lower() == selected_text:
                self.run_list.setCurrentItem(item)
        if self.run_list.count() and self.run_list.currentRow() < 0:
            self.run_list.setCurrentRow(0)

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
        path = self.selected_config_path()
        if not path:
            return
        self.detail_label.setText(f"Config: {path}")

    def _on_run_selected(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            return
        manifest = load_manifest(run_dir)
        lines = [f"Run: {run_dir.name}", f"Folder: {run_dir}"]
        if manifest.get("config_source"):
            lines.append(f"Config source: {manifest['config_source']}")
        if manifest.get("started_at"):
            lines.append(f"Started: {manifest['started_at']}")
        if manifest.get("completed_at"):
            lines.append(f"Completed: {manifest['completed_at']}")
        if manifest.get("status"):
            lines.append(f"Status: {manifest['status']}")
        self.detail_label.setText("\n".join(lines))

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
            [str(REPO_ROOT / "visualiser" / "amr_editor_main.py")],
            REPO_ROOT / "visualiser",
        )
        if not started:
            QMessageBox.critical(self, "Editor failed", "Could not start the editor.")
            return
        QMessageBox.information(
            self,
            "Open config",
            "The editor has opened. Use Open JSON in the editor and select:\n"
            f"{path}",
        )

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
        self.refresh_configs()
        self.detail_label.setText(f"Deleted launcher config:\n{path.name}")

    def run_selected_config(self) -> None:
        config = self.selected_config_path()
        if not config:
            QMessageBox.warning(self, "No config", "Select a config first.")
            return

        name_text = self.run_name_edit.text().strip()
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name_text)
        folder_name = f"{now_stamp()}_{safe_name}" if safe_name else now_stamp()
        run_dir = LAUNCHER_RUNS_DIR / folder_name
        run_dir.mkdir(parents=True, exist_ok=False)

        config_copy = run_dir / "config.json"
        shutil.copy2(config, config_copy)

        verbose_enabled = self.verbose_combo.currentIndex() == 0
        manifest = {
            "name": folder_name,
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

    def open_visualiser(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
            return
        started = self.start_detached(
            sys.executable,
            [str(REPO_ROOT / "visualiser" / "amr_sim_visualiser_pyside6.py")],
            REPO_ROOT / "visualiser",
        )
        if not started:
            QMessageBox.critical(self, "Visualiser failed", "Could not start the visualiser.")
            return
        manifest = load_manifest(run_dir)
        outputs = manifest.get("outputs", {})
        csv_path = outputs.get("visualiser_csv") or outputs.get("steps_csv")
        QMessageBox.information(
            self,
            "Open run in visualiser",
            "The visualiser has opened. Use Open Layout JSON and Open Simulation CSV with:\n\n"
            f"Layout JSON:\n{run_dir / 'config.json'}\n\n"
            f"Simulation CSV:\n{csv_path}",
        )

    def generate_report_for_selected_run(self) -> None:
        run_dir = self.selected_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "No run", "Select a run first.")
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
    app = QApplication.instance() or QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
