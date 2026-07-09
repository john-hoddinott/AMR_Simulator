from pathlib import Path
import json
import traceback

import pandas as pd

from amr_report_analysis import (
    analyse,
    load_amr_parameters,
    load_floor_dxf_map,
    load_location_catalog,
    load_payload_dimensions,
    load_payload_weights,
    load_task_generation_report_metadata,
)
from amr_report_pdf_report import (
    REPORT_SECTIONS,
    build_report,
    normalise_report_sections,
)
from amr_report_cli import parse_args


def load_run_manifest(csv_path: Path, out_path: Path) -> dict:
    candidates = [
        Path(out_path).parent / "run_manifest.json",
        Path(csv_path).parent / "run_manifest.json",
    ]
    for manifest_path in candidates:
        if not manifest_path.exists():
            continue
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def export_failed_tasks_csv(source_csv: Path, output_csv: Path) -> None:
    df = pd.read_csv(source_csv, low_memory=False)
    event_col = "event_type" if "event_type" in df.columns else ""
    if not event_col:
        failed = pd.DataFrame()
    else:
        failed = df[df[event_col].astype(str).str.fullmatch("task_failed", case=False, na=False)].copy()

    rows = []
    for _, row in failed.iterrows():
        context = {}
        raw_context = str(row.get("failure_context", "") or "").strip()
        if raw_context:
            try:
                parsed = json.loads(raw_context)
                if isinstance(parsed, dict):
                    context = parsed
            except Exception:
                context = {}
        if context:
            rows.append(context)
            continue
        rows.append(
            {
                "sim_time_sec": row.get("sim_time_sec", ""),
                "sim_datetime": row.get("sim_datetime", ""),
                "task_id": row.get("task_id", ""),
                "reason": row.get("pending_reason", row.get("details", "")),
                "pickup": row.get("from_location", ""),
                "dropoff": row.get("to_location", ""),
                "payload": row.get("payload", ""),
                "payload_instance_id": row.get("payload_instance_id", ""),
                "task_source": row.get("task_source", ""),
                "department_id": row.get("department_id", ""),
                "waste_stream": row.get("waste_stream", ""),
                "container_type": row.get("container_type", ""),
            }
        )

    out_df = pd.DataFrame(rows)
    if out_df.empty:
        out_df = pd.DataFrame(
            columns=[
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
        )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)


def print_progress(current: int, total: int, message: str = "") -> None:
    total = max(total, 1)
    current = max(0, min(current, total))
    width = 40
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    percent = int(100 * current / total)
    print(f"\r[{bar}] {percent:3d}% {message:<40}", end="", flush=True)
    if current >= total:
        print()


def parse_report_sections(value: str) -> list:
    return normalise_report_sections(
        [part.strip() for part in str(value or "").split(",") if part.strip()]
    )


def select_report_sections_dialog(initial_sections=None):
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QDialogButtonBox,
            QHBoxLayout,
            QLabel,
            QListWidget,
            QListWidgetItem,
            QPushButton,
            QVBoxLayout,
        )
    except Exception as exc:
        raise RuntimeError(
            "PySide6 is required for --select-report-sections. "
            "Use --report-sections for non-interactive report generation."
        ) from exc

    class ReportSectionDialog(QDialog):
        def __init__(self, initial=None):
            super().__init__()
            self.setWindowTitle("Report sections")
            self.resize(520, 560)
            self.result_sections = None
            selected = set(normalise_report_sections(initial))

            layout = QVBoxLayout(self)
            label = QLabel("Select sections and move them into the order they should appear in the PDF.")
            label.setWordWrap(True)
            layout.addWidget(label)

            row = QHBoxLayout()
            self.list_widget = QListWidget()
            self.list_widget.setDragDropMode(QListWidget.InternalMove)
            for section_id, title in REPORT_SECTIONS:
                item = QListWidgetItem(f"{title}  [{section_id}]")
                item.setData(Qt.UserRole, section_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if section_id in selected else Qt.Unchecked)
                self.list_widget.addItem(item)
            row.addWidget(self.list_widget, 1)

            buttons_col = QVBoxLayout()
            up_btn = QPushButton("Move up")
            down_btn = QPushButton("Move down")
            all_btn = QPushButton("Select all")
            none_btn = QPushButton("Clear")
            up_btn.clicked.connect(lambda: self._move_current(-1))
            down_btn.clicked.connect(lambda: self._move_current(1))
            all_btn.clicked.connect(lambda: self._set_all(Qt.Checked))
            none_btn.clicked.connect(lambda: self._set_all(Qt.Unchecked))
            for btn in (up_btn, down_btn, all_btn, none_btn):
                buttons_col.addWidget(btn)
            buttons_col.addStretch(1)
            row.addLayout(buttons_col)
            layout.addLayout(row, 1)

            dialog_buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            dialog_buttons.accepted.connect(self.accept)
            dialog_buttons.rejected.connect(self.reject)
            layout.addWidget(dialog_buttons)

        def _move_current(self, delta: int):
            row = self.list_widget.currentRow()
            if row < 0:
                return
            new_row = max(0, min(self.list_widget.count() - 1, row + delta))
            if new_row == row:
                return
            item = self.list_widget.takeItem(row)
            self.list_widget.insertItem(new_row, item)
            self.list_widget.setCurrentRow(new_row)

        def _set_all(self, state):
            for index in range(self.list_widget.count()):
                self.list_widget.item(index).setCheckState(state)

        def accept(self):
            sections = []
            for index in range(self.list_widget.count()):
                item = self.list_widget.item(index)
                if item.checkState() == Qt.Checked:
                    sections.append(item.data(Qt.UserRole))
            self.result_sections = normalise_report_sections(sections)
            super().accept()

    app = QApplication.instance() or QApplication([])
    dialog = ReportSectionDialog(initial_sections)
    if dialog.exec() != QDialog.Accepted:
        raise SystemExit("Report cancelled.")
    return dialog.result_sections


def generate_report_from_paths(
    csv_path: Path,
    out_path: Path,
    config_json: Path = None,
    target_amr_util: float = 0.85,
    target_lift_util: float = 0.70,
    heatmap_workers: int = None,
    include_drawings: bool = True,
    failed_tasks_csv: Path = None,
    report_sections=None,
    progress_callback=None,
) -> None:
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    config_json = Path(config_json) if config_json else None

    def emit_progress(current: int, total: int, message: str = "") -> None:
        if progress_callback:
            progress_callback(current, total, message)
        else:
            print_progress(current, total, message)

    emit_progress(0, 100, "Loading configuration")

    payload_weights = (
        load_payload_weights(config_json) if config_json else {}
    )
    amr_parameters = (
        load_amr_parameters(config_json) if config_json else None
    )
    floor_dxf_map = (
        load_floor_dxf_map(config_json)
        if config_json and include_drawings
        else {}
    )
    location_catalog = (
        load_location_catalog(config_json) if config_json else None
    )
    payload_dimensions = (
        load_payload_dimensions(config_json) if config_json else None
    )
    task_generation_metadata = (
        load_task_generation_report_metadata(config_json)
        if config_json
        else None
    )

    emit_progress(15, 100, "Analysing simulation data")

    results = analyse(
        csv_path,
        target_amr_util,
        target_lift_util,
        payload_weights,
        amr_parameters,
        floor_dxf_map,
        location_catalog,
        payload_dimensions,
        task_generation_metadata,
    )

    if failed_tasks_csv:
        export_failed_tasks_csv(csv_path, Path(failed_tasks_csv))
        emit_progress(30, 100, f"Failed-task CSV written to {failed_tasks_csv}")

    emit_progress(35, 100, "Building PDF report")
    run_manifest = load_run_manifest(csv_path, out_path)

    def report_progress(current: int, total: int, message: str = "") -> None:
        # Map report build progress into the 35-100 range
        base = 35
        span = 65
        mapped = base + int(span * current / max(total, 1))
        emit_progress(mapped, 100, message)

    build_report(
        results,
        csv_path,
        out_path,
        progress_callback=report_progress,
        heatmap_workers=heatmap_workers,
        include_drawings=include_drawings,
        report_sections=report_sections,
        run_manifest=run_manifest,
    )

    emit_progress(100, 100, f"Report written to {out_path}")


def launch_report_generator_dialog(args=None) -> None:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QDialog,
            QFileDialog,
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QDoubleSpinBox,
            QSpinBox,
            QTextEdit,
            QVBoxLayout,
        )
    except Exception as exc:
        raise RuntimeError("PySide6 is required for --report-dialog.") from exc

    class ReportGeneratorDialog(QDialog):
        def __init__(self, seed_args=None):
            super().__init__()
            self.setWindowTitle("Generate AMR report")
            self.resize(760, 720)
            self._generating = False

            layout = QVBoxLayout(self)

            form = QFormLayout()
            self.csv_edit = QLineEdit(str(getattr(seed_args, "csv", "") or ""))
            self.json_edit = QLineEdit(str(getattr(seed_args, "config_json", "") or ""))
            output_seed = str(getattr(seed_args, "output", "") or "")
            if not output_seed and self.csv_edit.text().strip():
                csv_path = Path(self.csv_edit.text().strip())
                output_seed = str(csv_path.with_name(csv_path.stem + "_report.pdf"))
            self.output_edit = QLineEdit(output_seed)
            self.failed_csv_edit = QLineEdit(
                str(getattr(seed_args, "failed_tasks_csv", "") or "")
            )

            self._add_file_row(
                form,
                "Simulation CSV",
                self.csv_edit,
                "Simulation CSV",
                "CSV files (*.csv);;All files (*)",
                save=False,
            )
            self._add_file_row(
                form,
                "Config JSON",
                self.json_edit,
                "Layout/config JSON",
                "JSON files (*.json);;All files (*)",
                save=False,
            )
            self._add_file_row(
                form,
                "Output PDF",
                self.output_edit,
                "Output PDF",
                "PDF files (*.pdf);;All files (*)",
                save=True,
            )
            self._add_file_row(
                form,
                "Failed-task CSV",
                self.failed_csv_edit,
                "Failed-task CSV",
                "CSV files (*.csv);;All files (*)",
                save=True,
            )

            self.target_amr_spin = QDoubleSpinBox()
            self.target_amr_spin.setRange(0.01, 1.0)
            self.target_amr_spin.setSingleStep(0.05)
            self.target_amr_spin.setValue(float(getattr(seed_args, "target_amr_util", 0.85) or 0.85))
            self.target_lift_spin = QDoubleSpinBox()
            self.target_lift_spin.setRange(0.01, 1.0)
            self.target_lift_spin.setSingleStep(0.05)
            self.target_lift_spin.setValue(float(getattr(seed_args, "target_lift_util", 0.70) or 0.70))
            self.heatmap_workers_spin = QSpinBox()
            self.heatmap_workers_spin.setRange(0, 64)
            self.heatmap_workers_spin.setValue(int(getattr(seed_args, "heatmap_workers", 0) or 0))
            self.include_drawings_check = QCheckBox("Render DXF drawings behind heatmaps")
            self.include_drawings_check.setChecked(not bool(getattr(seed_args, "omit_drawings", False)))

            form.addRow("Target AMR utilisation", self.target_amr_spin)
            form.addRow("Target lift utilisation", self.target_lift_spin)
            form.addRow("Heatmap workers (0 = auto)", self.heatmap_workers_spin)
            form.addRow("", self.include_drawings_check)
            layout.addLayout(form)

            layout.addWidget(QLabel("Report sections"))
            section_row = QHBoxLayout()
            self.section_list = QListWidget()
            self.section_list.setDragDropMode(QListWidget.InternalMove)
            initial_sections = (
                parse_report_sections(getattr(seed_args, "report_sections", ""))
                if getattr(seed_args, "report_sections", "")
                else normalise_report_sections(None)
            )
            selected = set(initial_sections)
            ordered = list(initial_sections) + [
                section_id
                for section_id, _title in REPORT_SECTIONS
                if section_id not in selected
            ]
            title_by_id = dict(REPORT_SECTIONS)
            for section_id in ordered:
                item = QListWidgetItem(f"{title_by_id.get(section_id, section_id)}  [{section_id}]")
                item.setData(Qt.UserRole, section_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if section_id in selected else Qt.Unchecked)
                self.section_list.addItem(item)
            section_row.addWidget(self.section_list, 1)

            section_buttons = QVBoxLayout()
            for text, slot in [
                ("Move up", lambda: self._move_section(-1)),
                ("Move down", lambda: self._move_section(1)),
                ("Select all", lambda: self._set_all_sections(Qt.Checked)),
                ("Clear", lambda: self._set_all_sections(Qt.Unchecked)),
            ]:
                btn = QPushButton(text)
                btn.clicked.connect(slot)
                section_buttons.addWidget(btn)
            section_buttons.addStretch(1)
            section_row.addLayout(section_buttons)
            layout.addLayout(section_row, 1)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            layout.addWidget(self.progress)

            self.log_box = QTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setMinimumHeight(110)
            layout.addWidget(self.log_box)

            actions = QHBoxLayout()
            self.generate_btn = QPushButton("Generate PDF")
            self.close_btn = QPushButton("Close")
            self.generate_btn.clicked.connect(self.generate_report)
            self.close_btn.clicked.connect(self.reject)
            actions.addStretch(1)
            actions.addWidget(self.generate_btn)
            actions.addWidget(self.close_btn)
            layout.addLayout(actions)

            self.csv_edit.textChanged.connect(self._maybe_default_output_path)

        def _add_file_row(self, form, label, line_edit, title, filters, save=False):
            row = QHBoxLayout()
            row.addWidget(line_edit, 1)
            btn = QPushButton("Browse...")
            btn.clicked.connect(
                lambda checked=False: self._browse_file(line_edit, title, filters, save)
            )
            row.addWidget(btn)
            form.addRow(label, row)

        def _browse_file(self, line_edit, title, filters, save=False):
            if save:
                path, _ = QFileDialog.getSaveFileName(self, title, line_edit.text(), filters)
            else:
                path, _ = QFileDialog.getOpenFileName(self, title, line_edit.text(), filters)
            if path:
                if save and filters.startswith("PDF") and not path.lower().endswith(".pdf"):
                    path += ".pdf"
                line_edit.setText(path)

        def _maybe_default_output_path(self):
            if self.output_edit.text().strip():
                return
            csv_text = self.csv_edit.text().strip()
            if not csv_text:
                return
            csv_path = Path(csv_text)
            self.output_edit.setText(str(csv_path.with_name(csv_path.stem + "_report.pdf")))

        def _move_section(self, delta: int):
            row = self.section_list.currentRow()
            if row < 0:
                return
            new_row = max(0, min(self.section_list.count() - 1, row + delta))
            if row == new_row:
                return
            item = self.section_list.takeItem(row)
            self.section_list.insertItem(new_row, item)
            self.section_list.setCurrentRow(new_row)

        def _set_all_sections(self, state):
            for index in range(self.section_list.count()):
                self.section_list.item(index).setCheckState(state)

        def selected_sections(self):
            sections = []
            for index in range(self.section_list.count()):
                item = self.section_list.item(index)
                if item.checkState() == Qt.Checked:
                    sections.append(item.data(Qt.UserRole))
            return normalise_report_sections(sections)

        def _log(self, message: str):
            self.log_box.append(str(message or ""))
            QApplication.processEvents()

        def generate_report(self):
            if self._generating:
                return
            csv_path = Path(self.csv_edit.text().strip())
            output_text = self.output_edit.text().strip()
            output_path = Path(output_text) if output_text else None
            json_text = self.json_edit.text().strip()
            failed_text = self.failed_csv_edit.text().strip()

            if not csv_path.exists():
                QMessageBox.warning(self, "Missing CSV", "Select an existing simulation CSV.")
                return
            if json_text and not Path(json_text).exists():
                QMessageBox.warning(self, "Missing JSON", "Select an existing config JSON or leave it blank.")
                return
            if output_path is None:
                QMessageBox.warning(self, "Missing output", "Select an output PDF path.")
                return

            self._generating = True
            self.generate_btn.setEnabled(False)
            self.close_btn.setEnabled(False)
            self.progress.setValue(0)
            self.log_box.clear()

            def on_progress(current: int, total: int, message: str = ""):
                total = max(1, int(total or 1))
                value = max(0, min(100, int(100 * int(current or 0) / total)))
                self.progress.setValue(value)
                if message:
                    self._log(message)

            try:
                generate_report_from_paths(
                    csv_path=csv_path,
                    out_path=output_path,
                    config_json=Path(json_text) if json_text else None,
                    target_amr_util=float(self.target_amr_spin.value()),
                    target_lift_util=float(self.target_lift_spin.value()),
                    heatmap_workers=(
                        int(self.heatmap_workers_spin.value())
                        if self.heatmap_workers_spin.value() > 0
                        else None
                    ),
                    include_drawings=self.include_drawings_check.isChecked(),
                    failed_tasks_csv=Path(failed_text) if failed_text else None,
                    report_sections=self.selected_sections(),
                    progress_callback=on_progress,
                )
                QMessageBox.information(self, "Report complete", f"Report written to:\n{output_path}")
            except Exception as exc:
                QMessageBox.critical(self, "Report failed", str(exc))
                self._log(traceback.format_exc())
            finally:
                self._generating = False
                self.generate_btn.setEnabled(True)
                self.close_btn.setEnabled(True)

    app = QApplication.instance() or QApplication([])
    dialog = ReportGeneratorDialog(args)
    dialog.exec()


def main() -> None:
    args = parse_args()
    if args.list_report_sections:
        for section_id, title in REPORT_SECTIONS:
            print(f"{section_id}: {title}")
        return

    if args.report_dialog:
        launch_report_generator_dialog(args)
        return

    if not args.csv:
        raise SystemExit(
            "CSV path is required unless --list-report-sections or --report-dialog is used."
        )

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    out_path = (
        Path(args.output)
        if args.output
        else csv_path.with_name(csv_path.stem + "_report.pdf")
    )

    report_sections = (
        parse_report_sections(args.report_sections) if args.report_sections else None
    )
    if args.select_report_sections:
        report_sections = select_report_sections_dialog(report_sections)

    generate_report_from_paths(
        csv_path=csv_path,
        out_path=out_path,
        config_json=Path(args.config_json) if args.config_json else None,
        target_amr_util=args.target_amr_util,
        target_lift_util=args.target_lift_util,
        heatmap_workers=args.heatmap_workers,
        include_drawings=not args.omit_drawings,
        failed_tasks_csv=Path(args.failed_tasks_csv) if args.failed_tasks_csv else None,
        report_sections=report_sections,
    )


if __name__ == "__main__":
    main()
