"""MainWindow's MRT drawing (Edit Network's MRT sub-tab): deploy a line stop by stop, the crossing-line merge
question, the leg preview, the Finish MRT line dialog and the lines/load summary. Cut out of main.py's MainWindow as
a plain mixin; _accept_metro_line and _merge_or_discard stay methods looked up on self (tests replace them on the
window). Its metro_line is made in MainWindow._build_ui; it calls _end_modes_except, _commit, _refresh_mode and
_show_hint through self."""

from __future__ import annotations

import math

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QMessageBox, QSpinBox,
                               QVBoxLayout)

from traffic_simulator.institutions import metro_report
from traffic_simulator.models import METRO_MIN_SPACING_M, METRO_SERVICE, MetroCrossing
from traffic_simulator.widgets import hint_label


class MetroMixin:
    """Deploying MRT lines on the map."""

    # What it uses of the window and doesn't define: MainWindow's state and the other mixins' methods
    # (tests/test_docs.py keeps this exact, so a new dependency shows in the diff)
    NEEDS = ('_commit', '_end_modes_except', '_network_summary', '_refresh_mode', '_show_hint', 'metro_button',
             'metro_line', 'metro_summary', 'network', 'od_matrix', 'scene', 'statusBar')

    def toggle_metro(self, checked: bool) -> None:
        """Start deployment: every map click a metro stop, the second making a new line and each one after it
        extending it; unchecked (a right-click, Esc, another tool or tab) the deployment ends and the line is
        accepted, final (one undo step; no later edits), or deleted (`_accept_metro_line`). A first stop with no
        second is dropped: a line needs track."""
        if checked:
            self._end_modes_except(self.metro_button)
        elif self.metro_line in self.network.metro_lines:
            line = self.network.metro_lines[self.metro_line]
            summary = f"{line.id}: {len(line.stops)} stops, {self.network.metro_length_m(line.id) / 1000:.2f} km of track"
            service = self._accept_metro_line(summary)
            if service is not None:
                for name, value in service.items():
                    setattr(line, name, value)
                self.statusBar().showMessage(f"{self._network_summary()} • MRT {summary}, a train every "
                                             f"{line.headway_min:g} min, accepted")
                self._commit()
            else:
                self.network.delete_metro_line(line.id)
                self.statusBar().showMessage(f"{self._network_summary()} • MRT {line.id} deleted")
        self.metro_line, self.scene.metro_pending = None, None
        self.scene.metro_deploying = checked
        self.scene.rebuild()
        self._refresh_mode()

    def metro_clicked(self, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> None:
        """A stop where the map was clicked while deploying: the first waits for the second, which makes the
        line; then each joins the line's last stop, aimed along (dx, dy) when the click was dragged (the track runs
        through the stop that way; (0, 0): a plain click). Refused (too near the last stop, an aim that can't be met)
        says why and changes nothing.
        Nothing is an undo step until the line is accepted (toggle_metro)."""
        if self.metro_line is not None and self.metro_line not in self.network.metro_lines:
            self.metro_line = None  # undone while deploying: start a new line
        merge = False
        while True:
            try:
                if self.metro_line is not None:
                    self.network.add_metro_stop(self.metro_line, x, y, merge, (dx, dy) if dx or dy else None)
                elif self.scene.metro_pending is None:
                    self.scene.metro_pending = (x, y)
                else:
                    self.metro_line = self.network.add_metro_line(self.scene.metro_pending, (x, y), merge).id
                    self.scene.metro_pending = None
                break
            except MetroCrossing as crossing:  # it crosses another line: an interchange there, or not this line
                choice = self._merge_or_discard(crossing.line_id)
                if choice == "merge":
                    merge = True
                    continue
                if choice == "discard":
                    if self.metro_line is not None:
                        self.network.delete_metro_line(self.metro_line)
                    self.metro_line, self.scene.metro_pending = None, None
                    self.scene.rebuild()
                    self._refresh_mode()
                    self._show_hint("Line discarded. Click to start another, or right-click to finish.")
                return
            except ValueError as exc:
                self._show_hint(f"{exc} Click elsewhere, or right-click to finish.")
                return
        self.scene.rebuild()
        self._refresh_mode()

    def _merge_or_discard(self, line_id: str) -> str | None:
        """A leg crossing line `line_id`: "merge" (an interchange station at the crossing, on both lines), "discard"
        (the line being drawn goes) or None (Cancel: not this stop)."""
        box = QMessageBox(QMessageBox.Icon.Question, "Lines Cross",
                          f"This leg crosses line {line_id}. Merge them at an interchange station where they cross "
                          "(riders can change lines there), or discard the line you are drawing?", parent=self)
        merge = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard Line", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return {merge: "merge", discard: "discard"}.get(box.clickedButton())

    def _metro_ghost(self, x: float, y: float, dx: float = 0.0, dy: float = 0.0) -> None:
        """While deploying, the leg a click at (x, y) would add (Network.metro_leg: curved, drawn in if too sharp),
        before it is placed: amber if it crosses another line, red if refused."""
        points, state = [], "ok"
        if self.metro_line in self.network.metro_lines:
            start = self.network.metro_track(self.metro_line)[-1]
            try:
                leg, crossing = self.network.metro_leg(self.metro_line, x, y, (dx, dy) if dx or dy else None)
                points, state = [start, *leg], "crossing" if crossing else "ok"
            except ValueError:
                points, state = [start, (x, y)], "refused"
        elif self.scene.metro_pending is not None:
            px, py = self.scene.metro_pending
            points = [(px, py), (x, y)]
            state = "ok" if math.hypot(x - px, y - py) >= METRO_MIN_SPACING_M else "refused"
        self.scene.show_metro_ghost(points, state, (x, y, dx, dy) if dx or dy else None)

    def _accept_metro_line(self, summary: str) -> dict | None:
        """The finished line: accepted with its service (headway, speed, carriages a train and passengers a carriage,
        fare: assumed values until changed, METRO_SERVICE) as final, never edited after; or deleted (None)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Finish MRT line")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"MRT {summary}. Accept it as final, or delete it?"))
        note = hint_label("Assumed service: set this line's own.")
        layout.addWidget(note)
        form, boxes = QFormLayout(), {}
        for name, label, suffix in (("headway_min", "A train every", " min"), ("speed_kmh", "Speed, stops included",
                                                                               " km/h"),
                                    ("cars_per_train", "Carriages a train", ""),
                                    ("car_capacity", "Passengers a carriage", ""), ("fare_cents", "Fare", " ¢")):
            default, low, high = METRO_SERVICE[name]
            box = QSpinBox() if isinstance(default, int) else QDoubleSpinBox()
            box.setRange(low, high)
            box.setSuffix(suffix)
            box.setValue(default)
            form.addRow(label, box)
            boxes[name] = box
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        accept = buttons.addButton("Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole).clicked.connect(dialog.reject)
        accept.clicked.connect(dialog.accept)
        accept.setDefault(True)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return {name: box.value() for name, box in boxes.items()}

    def _show_metro_summary(self) -> None:
        """Lines, stops and track; and each line's busiest segment at the design hour against its capacity."""
        network = self.network
        lines = len(network.metro_lines)
        if not lines:
            self.metro_summary.setText("No MRT lines yet")
            return
        km = sum(network.metro_length_m(line_id) for line_id in network.metro_lines) / 1000
        text = [f"{lines} line{'s' * (lines != 1)}, {len(network.metro_stops)} stops, {km:.2f} km of track"]
        report = self._metro_report()
        for row in report["lines"]:
            if row["riders_per_h"]:
                text.append(f"{row['line']}: {row['peak_load_per_h']:,} an hour at its busiest ({row['peak_segment']}), "
                            f"{row['v_c']:.0%} of {row['capacity_per_h_each_way']:,}"
                            + (" – over capacity" if row["over_capacity"] else ""))
        self.metro_summary.setText("\n".join(text))

    def _metro_report(self) -> dict:
        """metro_report: the metro's design-hour passengers on their lines (institutions.metro_report)."""
        return metro_report(self.network, self.od_matrix.mode == "daily")
