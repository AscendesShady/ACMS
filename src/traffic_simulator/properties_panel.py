"""Inspector/editor widget for selected model objects."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPixmap, QShortcut,
                           QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .interchanges import fit_slip_roads
from .models import MAX_BOARDING_PER_H, MAX_DWELL_S, BusStop, Network, Node, Road
from .roundabouts import design_roundabout
from .signal_timing import SATURATION_PER_LANE_VPH
from .theme import TEXT

# Icons are glyphs from Windows' own icon font: Segoe Fluent Icons on Windows 11, Segoe MDL2
# Assets on Windows 10, same code points.
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


# The signal timing methods (models.SIGNAL_METHODS) as the junction panel and Generate Network name them.
TIMING_METHODS = (("Webster", "webster"), ("TRANSYT", "transyt"), ("Synchro", "synchro"), ("SCATS", "scats"),
                  ("SCOOT", "scoot"), ("Actuated", "actuated"), ("Max pressure", "max_pressure"), ("Fixed time", "fixed"))


def glyph_icon(glyph: str, family: str, px: int = 20) -> QIcon:
    """A white icon from one font glyph, drawn at 1x, 1.5x and 2x so it stays crisp on any display."""
    icon = QIcon()
    for size in (px, px * 3 // 2, px * 2):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        font = QFont(family)
        font.setPixelSize(size)
        painter.setFont(font)
        painter.setPen(QColor(TEXT))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def icon_button(glyph: int, name: str, role: str = "") -> QPushButton:
    """A small button showing one icon glyph, its name as the tooltip (and for screen readers): a few
    words, never an explanation. Without an icon font it shows its name."""
    button = styled_button("", role)
    button.setProperty("glyph", True)  # square, in the app stylesheet
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    set_glyph(button, glyph, name)
    return button


MINI_ICON_PX = 12  # a mini icon button's glyph (role "mini")


def set_glyph(button: QPushButton, glyph: int, name: str) -> None:
    """Give an icon button another glyph and name (e.g. Start becoming Restart)."""
    family = next((family for family in ICON_FONTS if family in QFontDatabase.families()), None)
    button.setText("" if family else name)
    button.setToolTip(name)
    button.setAccessibleName(name)
    if family:
        button.setIcon(glyph_icon(chr(glyph), family, 16))


class Collapsible(QWidget):
    """A section under a title that opens and closes it, a chevron showing which (right: closed,
    down: open). `body` is laid out under the title, indented."""

    def __init__(self, title: str, body: QWidget, expanded: bool = False, tooltip: str = "") -> None:
        super().__init__()
        family = next((family for family in ICON_FONTS if family in QFontDatabase.families()), None)
        self._title = title
        self._icons = (glyph_icon("\ue76c", family, 12), glyph_icon("\ue70d", family, 12)) if family else None
        self.header = styled_button(title, "section", tooltip)
        self.header.setCheckable(True)
        self.header.setAccessibleName(title)
        self.header.toggled.connect(self._show)
        self.body = body
        if body.layout() is not None:
            body.layout().setContentsMargins(10, 6, 0, 10)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(body)
        self.header.setChecked(expanded)
        self._show(expanded)

    def _show(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.updateGeometry()  # so the panels holding it (and a scroll area) re-measure
        if self._icons:
            self.header.setIcon(self._icons[expanded])
        else:
            self.header.setText(("▾ " if expanded else "▸ ") + self._title)


class OpenPageTabs(QTabWidget):
    """Tabs as tall as the open page rather than the tallest one. QTabWidget sizes itself for its
    tallest page, hidden ones included, in its size hints and, when pages wrap text, in its height for
    a width; this swaps that page for the open one in all three, so a scroll area round the tabs
    scrolls only when the open page needs it, and a short page isn't stretched past the window."""

    def __init__(self) -> None:
        super().__init__()
        self.currentChanged.connect(self.updateGeometry)

    def tabInserted(self, index: int) -> None:  # type intentionally inherited from Qt
        super().tabInserted(index)
        self.widget(index).installEventFilter(self)  # a page's layout changed (a section opened): re-measure

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        if event.type() == QEvent.Type.LayoutRequest and watched is self.currentWidget():
            self.updateGeometry()
        return False

    def _open_page(self, hint: QSize, size) -> QSize:
        """`hint` with the tallest page's height (`size` of each page) swapped for the open page's."""
        page = self.currentWidget()
        if page is None:
            return hint
        tallest = max(size(self.widget(index)).height() for index in range(self.count()))
        return QSize(hint.width(), hint.height() - tallest + size(page).height())

    def sizeHint(self) -> QSize:  # type intentionally inherited from Qt
        return self._open_page(super().sizeHint(), QWidget.sizeHint)

    def minimumSizeHint(self) -> QSize:  # type intentionally inherited from Qt
        return self._open_page(super().minimumSizeHint(), QWidget.minimumSizeHint)

    def heightForWidth(self, width: int) -> int:  # type intentionally inherited from Qt
        total, page = super().heightForWidth(width), self.currentWidget()
        if total < 0 or page is None:
            return total
        inner = width - max(self.width() - page.width(), 0) if page.width() else width  # the pages' width
        tallest = max(max(self.widget(index).heightForWidth(inner) for index in range(self.count())), 0)
        mine = page.heightForWidth(inner)
        return total - tallest + (mine if mine >= 0 else page.minimumSizeHint().height())


def describe_group(objects: list[tuple[str, str]]) -> str:
    """'3 junctions, 1 road, 2 bus stops': a selection's objects counted by kind."""
    names = {"node": "junction", "road": "road", "end": "boundary end", "bus_stop": "bus stop"}
    counts = [(sum(kind == name for kind, _ in objects), single) for name, single in names.items()]
    return ", ".join(f"{count} {single}{'s' if count > 1 else ''}" for count, single in counts if count)


def styled_button(text: str, role: str = "", tooltip: str = "") -> QPushButton:
    """Button tagged for the app stylesheet: role "primary" (one per area) or "danger"."""
    button = QPushButton(text)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # focus ring for keyboard use, not after clicks
    if role:
        button.setProperty("role", role)
    if tooltip:
        button.setToolTip(tooltip)
    return button


def align_form_labels(container: QWidget) -> None:
    """Give every form inside `container` the same label width, so the fields of all its
    sections start on one line. Call once the widgets use the app's stylesheet font."""
    labels = []
    for form in container.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None:
                labels.append(item.widget())
    for label in labels:
        label.ensurePolished()
    width = max((label.sizeHint().width() for label in labels), default=0)
    for label in labels:
        label.setMinimumWidth(width)


def hint_label(text: str = "") -> QLabel:
    """Secondary explanatory text, styled by the app stylesheet at readable contrast."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", "hint")
    return label


class ChoiceButtons(QWidget):
    """Radio buttons for a short list of options (fewer than 5 read better than a dropdown)."""

    changed = Signal()  # the user picked an option; programmatic set_value() stays quiet

    def __init__(self, options: list[tuple[str, object]], vertical: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.values = [value for _, value in options]
        self.group = QButtonGroup(self)
        for index, (label, _) in enumerate(options):
            button = QRadioButton(label)
            self.group.addButton(button, index)
            layout.addWidget(button)
        if not vertical:
            layout.addStretch(1)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(lambda _: self.changed.emit())

    def value(self) -> object:
        return self.values[self.group.checkedId()]

    def set_value(self, value: object) -> None:
        self.group.button(self.values.index(value)).setChecked(True)

    def set_labels(self, labels: list[str]) -> None:
        for button, label in zip(self.group.buttons(), labels):
            button.setText(label)


class CheckList(QComboBox):
    """A dropdown of ticks: each option is a checkbox, the list stays open while ticking, and the box shows
    what is ticked. `before_open()` runs as it opens (to list what is there now). A long list scrolls: up to
    MAX_SHOWN rows show at once, with a scroll bar (and the wheel). A click toggles its row once, on release; the
    popup never sees it (it would toggle the row again, or close the list)."""

    changed = Signal()
    MAX_SHOWN = 10

    def __init__(self, before_open=None) -> None:
        super().__init__()
        self.before_open = before_open
        self.setModel(QStandardItemModel(self))
        # Fusion's menu-like popup ignores maxVisibleItems and runs off the screen: a plain list that scrolls
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")
        self.setMaxVisibleItems(self.MAX_SHOWN)
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view().viewport().installEventFilter(self)  # after the popup's own filter, so it runs first

    def labels(self) -> list[str]:
        model = self.model()
        return [model.item(row).text() for row in range(model.rowCount())]

    def ticked(self) -> list[str]:
        model = self.model()
        return [model.item(row).text() for row in range(model.rowCount())
                if model.item(row).checkState() == Qt.CheckState.Checked]

    def summary(self) -> str:
        return ", ".join(self.ticked()) or "none"

    def set_options(self, labels: list[str], ticked) -> None:
        """List `labels`, ticking those in `ticked`."""
        model = self.model()
        model.clear()
        for label in labels:
            item = QStandardItem(label)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if label in ticked else Qt.CheckState.Unchecked)
            model.appendRow(item)
        self.update()

    def set_ticked(self, labels: list[str], check_only: bool = False) -> None:
        """Tick exactly these (by label, any case); ValueError naming any it doesn't list."""
        known = {label.lower(): label for label in self.labels()}
        unknown = [label for label in labels if label.lower() not in known]
        if unknown:
            raise ValueError(f"Not listed: {', '.join(unknown)}. Listed: {', '.join(known.values())}.")
        if not check_only:
            self.set_options(self.labels(), {known[label.lower()] for label in labels})
            self.changed.emit()

    def eventFilter(self, watched, event) -> bool:  # type intentionally inherited from Qt
        """The list's clicks: a left button's release toggles the row under it; press, release and double
        click stop here."""
        clicks = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)
        if watched is self.view().viewport() and event.type() in clicks:
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                index = self.view().indexAt(event.position().toPoint())
                if index.isValid():
                    self._toggle(index)
            return True
        return super().eventFilter(watched, event)

    def _toggle(self, index) -> None:
        item = self.model().itemFromIndex(index)
        ticked = item.checkState() == Qt.CheckState.Checked
        item.setCheckState(Qt.CheckState.Unchecked if ticked else Qt.CheckState.Checked)
        self.update()
        self.changed.emit()

    def showPopup(self) -> None:
        if self.before_open is not None:
            self.before_open()
        super().showPopup()

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.summary()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


def _on_enter(section: QWidget, action) -> None:
    """Enter (main or keypad) anywhere inside the section runs `action`."""
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        shortcut = QShortcut(QKeySequence(key), section)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(action)


def _section() -> tuple[QWidget, QFormLayout]:
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    return widget, form


class PropertiesPanel(QWidget):
    network_changed = Signal()
    interchange_requested = Signal(str)  # "trumpet", "diamond" or "spui"
    edited = Signal(object)  # a change that needs no redraw (signal settings); arg = undo merge key
    bus_stop_changed = Signal(str)  # a bus stop added, moved or re-pointed: select it, redraw, one undo step
    delete_group = Signal()  # Delete Selected: the box's selection, after asking
    slip_road_requested = Signal(str, str, bool)  # Add Slip Road: the two selected roads, single lane?

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.network: Network | None = None
        self.kind = ""
        self.selected: Road | Node | None = None

        self.group = QGroupBox("Selection")
        layout = QVBoxLayout(self.group)
        self.empty_hint = hint_label("Nothing selected. Click a road, a junction or an "
                                     "end arrow on the map to edit it. Ctrl+click junctions "
                                     "to pick several: 3 make a trumpet, 4 a diamond or SPUI.")
        layout.addWidget(self.empty_hint)
        self.header, header = _section()
        self.object_type = QLabel()
        self.object_type.setWordWrap(True)
        self.object_type.setStyleSheet("font-weight: 600;")
        self.object_id = QLabel()
        header.addRow("Object", self.object_type)
        header.addRow("ID", self.object_id)
        layout.addWidget(self.header)
        self.drag_button = styled_button(
            "Drag to Adjust", tooltip="Drag selection on map")
        self.drag_button.setCheckable(True)
        self.delete_node_button = styled_button(
            "Delete Node…", "danger", "Delete junction and roads (Del)")
        self.delete_node_button.clicked.connect(self.delete_node)
        # A road: add a bus stop, choosing from the menu (filled for each road) the traffic it serves.
        self.bus_stop_button = icon_button(
            0xE806, "Add Bus Stop")  # bus glyph
        self.bus_stop_menu = QMenu(self.bus_stop_button)
        self.bus_stop_button.setMenu(self.bus_stop_menu)
        self.delete_stop_button = icon_button(0xE74D, "Delete Bus Stop",
                                              "danger")  # bin glyph
        self.delete_stop_button.clicked.connect(self.delete_bus_stop)
        object_buttons = QHBoxLayout()
        object_buttons.addWidget(self.drag_button)
        object_buttons.addWidget(self.delete_node_button)
        object_buttons.addWidget(self.bus_stop_button)
        layout.addLayout(object_buttons)

        self.road_fields, form = _section()
        self.length = QDoubleSpinBox()
        self.length.setRange(0.1, 1_000_000.0)
        self.length.setDecimals(2)
        self.length.setSuffix(" m")
        self.lanes = QSpinBox()
        self.lanes.setRange(1, 12)
        self.lane_changing = QCheckBox("Allowed")
        self.speed = QComboBox()
        self.speed.setEditable(True)
        self.speed.addItems(["20", "30", "40", "50", "60", "70", "80", "100", "120"])
        self.speed.setCurrentText("50")
        # Labels are filled per road with its node IDs; values are the option index.
        self.direction = ChoiceButtons([("Two-way", 0), ("One-way", 1), ("One-way, reversed", 2)],
                                       vertical=True)
        self.direction.setToolTip("One or two directions")
        self.curve_radius = QLabel()  # tightest bend of a curved road, from its stored curvature
        self.curve_radius.setToolTip("Bend radius")
        self.road_type = QComboBox()
        self.road_type.setEditable(True)
        self.road_type.addItems(["urban", "arterial", "collector", "local", "bus-priority", "ramp"])
        road_type_note = hint_label("Road type is a label only. It is saved with the network "
                                    "but does not change geometry, speed or behaviour yet.")
        self.apply_button = styled_button("Apply", "primary")
        self.apply_button.clicked.connect(self.apply_changes)
        self.delete_button = styled_button("Delete Road…", "danger", "Delete this road (Del)")
        self.delete_button.clicked.connect(self.delete_road)
        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.delete_button)
        for label, widget in (
            ("Length", self.length), ("Lanes / direction", self.lanes),
            ("Lane changing", self.lane_changing), ("Speed limit (km/h)", self.speed),
            ("Direction", self.direction), ("Tightest curve", self.curve_radius),
            ("Road type", self.road_type),
        ):
            form.addRow(label, widget)
        form.addRow(road_type_note)
        form.addRow(buttons)
        # Apply is enabled only while the fields differ from the road, so it's clear
        # whether edits are pending; Enter applies them too.
        for signal in (self.length.valueChanged, self.lanes.valueChanged, self.lane_changing.toggled,
                       self.speed.currentTextChanged, self.direction.changed,
                       self.road_type.currentTextChanged):
            signal.connect(self._refresh_apply)
        _on_enter(self.road_fields, self.apply_changes)

        self.node_fields, form = _section()
        self.control = ChoiceButtons([("Signalised", "signalised"), ("Roundabout", "roundabout"),
                                      ("Uncontrolled", "uncontrolled")], vertical=True)
        self.control.setToolTip("How the junction is controlled")
        self.control.changed.connect(self.apply_control)
        form.addRow("Control", self.control)

        # Roundabout geometry, designed from the approach roads (read-only).
        self.roundabout_fields, form = _section()
        self.roundabout_info = {}
        for key, label in (("type", "Type"), ("speed", "Design speed"), ("diameter", "Inscribed diameter"),
                           ("island", "Central island"), ("circulating", "Circulating speed")):
            self.roundabout_info[key] = QLabel()
            form.addRow(label, self.roundabout_info[key])
        form.addRow(hint_label("Sized from the approach speed limit and lanes, following NCHRP "
                               "Report 672 (the roundabout design guide the HCM relies on). "
                               "It resizes when those roads change."))

        # Signal timing for signalised junctions: runs use it (simulation.py).
        self.signal_fields, form = _section()
        self.run_plans: dict = {}  # the run's signal plans (live for SCATS and SCOOT), shown here
        self.phases = ChoiceButtons([("2 phases", 2), ("4 phases", 4)])
        self.phases.changed.connect(self.apply_phases)
        self.timing_method = QComboBox()
        for label, method in TIMING_METHODS:
            self.timing_method.addItem(label, method)
        self.timing_method.setToolTip(
            "Signal timing method")
        self.timing_method.currentIndexChanged.connect(self.apply_timing_method)
        form.addRow("Phases", self.phases)
        form.addRow("Timing method", self.timing_method)
        # The coordinated methods' settings: each shown only for the methods that use it.
        self.cycle_min, self.cycle_max = QDoubleSpinBox(), QDoubleSpinBox()
        cycle_limits = QWidget()
        limits = QHBoxLayout(cycle_limits)
        limits.setContentsMargins(0, 0, 0, 0)
        for spin, name in ((self.cycle_min, "cycle_min_s"), (self.cycle_max, "cycle_max_s")):
            spin.setRange(20.0, 240.0)
            spin.setDecimals(0)
            spin.setSuffix(" s")
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda value, name=name: self.apply_timing_setting(name, value))
        limits.addWidget(self.cycle_min)
        limits.addWidget(QLabel("to"))
        limits.addWidget(self.cycle_max)
        cycle_limits.setToolTip("Group's cycle limits")
        self.target_saturation = QSpinBox()
        self.target_saturation.setRange(50, 110)
        self.target_saturation.setSuffix(" %")
        self.target_saturation.setKeyboardTracking(False)
        self.target_saturation.setToolTip("Target degree of saturation")
        self.target_saturation.valueChanged.connect(
            lambda value: self.apply_timing_setting("target_saturation", value / 100))
        self.stop_weight = QDoubleSpinBox()
        self.stop_weight.setRange(0.0, 120.0)
        self.stop_weight.setDecimals(0)
        self.stop_weight.setSuffix(" s")
        self.stop_weight.setKeyboardTracking(False)
        self.stop_weight.setToolTip("Delay one stop counts")
        self.stop_weight.valueChanged.connect(lambda value: self.apply_timing_setting("stop_weight_s", value))
        self.timing_settings = {}  # label -> (widget, the methods that use it)
        for text, widget, methods in (("Cycle limits", cycle_limits, ("transyt", "synchro", "scats", "scoot")),
                                      ("Target saturation", self.target_saturation, ("scats", "scoot")),
                                      ("Stop weight", self.stop_weight, ("transyt", "scoot"))):
            label = QLabel(text)
            form.addRow(label, widget)
            self.timing_settings[label] = (widget, methods)
        self.green_times = []
        for phase in range(4):
            label = QLabel(f"Phase {phase + 1} green")
            green = QDoubleSpinBox()
            green.setRange(5.0, 180.0)
            green.setDecimals(0)
            green.setSuffix(" s")
            green.valueChanged.connect(lambda value, phase=phase: self.apply_green(phase, value))
            form.addRow(label, green)
            self.green_times.append((label, green))
        self.signal_note = hint_label()
        form.addRow(self.signal_note)

        # Several junctions picked with Ctrl+click: offer the interchanges they can form.
        self.multi_fields = QWidget()
        multi_layout = QVBoxLayout(self.multi_fields)
        multi_layout.setContentsMargins(0, 0, 0, 0)
        self.multi_hint = hint_label()
        multi_layout.addWidget(self.multi_hint)
        self.interchange_buttons = {}
        for kind, text, tooltip in (
            ("trumpet", "Prepare Trumpet Interchange…", "Three junctions: a trumpet"),
            ("diamond", "Prepare Diamond Interchange…", "Ramps meet at two signals"),
            ("spui", "Prepare SPUI…", "Single-point urban interchange"),
        ):
            button = styled_button(text, tooltip=tooltip)
            button.clicked.connect(lambda _=False, kind=kind: self.interchange_requested.emit(kind))
            multi_layout.addWidget(button)
            self.interchange_buttons[kind] = button

        # A bus stop: the traffic it serves, and where its bay's middle sits from each end of its road.
        self.stop_fields, form = _section()
        self.stop_serves = ChoiceButtons([("Forward", "forward"), ("Backward", "backward"),
                                          ("Both directions", "both")], vertical=True)
        self.stop_serves.setToolTip("Directions it serves")
        self.stop_serves.changed.connect(self.apply_stop_direction)
        self.stop_from_label, self.stop_to_label = QLabel(), QLabel()
        self.stop_from, self.stop_to = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.stop_from, self.stop_to):
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(" m")
            spin.setKeyboardTracking(False)  # moves on Enter or an arrow step, not on every digit typed
            spin.setToolTip("Distance along the road")
        self.stop_from.valueChanged.connect(self.apply_stop_from)
        self.stop_to.valueChanged.connect(self.apply_stop_to)
        self.stop_dwell = QDoubleSpinBox()
        self.stop_dwell.setRange(0.0, MAX_DWELL_S)
        self.stop_dwell.setDecimals(0)
        self.stop_dwell.setSuffix(" s")
        self.stop_dwell.setKeyboardTracking(False)
        self.stop_dwell.setToolTip("Bus waiting time")
        self.stop_dwell.valueChanged.connect(self.apply_stop_dwell)
        self.stop_boarding = QDoubleSpinBox()
        self.stop_boarding.setRange(0.0, MAX_BOARDING_PER_H)
        self.stop_boarding.setDecimals(0)
        self.stop_boarding.setSuffix(" /h")
        self.stop_boarding.setKeyboardTracking(False)
        self.stop_boarding.setToolTip("Passengers boarding per hour")
        self.stop_boarding.valueChanged.connect(self.apply_stop_passengers)
        self.stop_alighting = QDoubleSpinBox()
        self.stop_alighting.setRange(0.0, 100.0)
        self.stop_alighting.setDecimals(0)
        self.stop_alighting.setSuffix(" %")
        self.stop_alighting.setKeyboardTracking(False)
        self.stop_alighting.setToolTip("Share of load alighting")
        self.stop_alighting.valueChanged.connect(self.apply_stop_passengers)
        form.addRow("Serves", self.stop_serves)
        form.addRow(self.stop_from_label, self.stop_from)
        form.addRow(self.stop_to_label, self.stop_to)
        form.addRow("Dwell time", self.stop_dwell)
        form.addRow("Boarding", self.stop_boarding)
        form.addRow("Alighting", self.stop_alighting)
        form.addRow(self.delete_stop_button)

        # A selection box's objects: what it took, and Delete Selected.
        self.group_fields = QWidget()
        group_row = QHBoxLayout(self.group_fields)
        group_row.setContentsMargins(0, 0, 0, 0)
        self.delete_group_button = icon_button(0xE74D, "Delete Selected",
                                               "danger")
        self.delete_group_button.clicked.connect(self.delete_group.emit)
        group_row.addWidget(self.delete_group_button)
        # Two roads meeting at a junction: a slip road for the kerb-side turn between them.
        self.slip_road_button = icon_button(0xE816, "Add Slip Road")
        slip_menu = QMenu(self.slip_road_button)
        for text, single in (("Single lane", True), ("Two lanes", False)):
            slip_menu.addAction(text, lambda _=False, single=single: self.slip_road_requested.emit(*self._slip_roads, single))
        self.slip_road_button.setMenu(slip_menu)
        group_row.addWidget(self.slip_road_button)
        group_row.addStretch(1)

        self.flow_fields, form = _section()
        self.flow_label = QLabel()
        self.flow = QDoubleSpinBox()
        self.flow.setRange(0.0, 100_000.0)
        self.flow.setDecimals(0)
        self.flow.setSingleStep(50)
        self.flow.setSuffix(" veh/h")
        self.flow_apply = styled_button("Apply", "primary")
        self.flow_apply.clicked.connect(self.apply_flow)
        form.addRow(self.flow_label, self.flow)
        form.addRow(self.flow_apply)
        _on_enter(self.flow_fields, self.apply_flow)

        for section in (self.road_fields, self.node_fields, self.roundabout_fields, self.signal_fields,
                        self.stop_fields, self.flow_fields, self.multi_fields, self.group_fields):
            layout.addWidget(section)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.group)
        self.show_object("", None)

    def set_network(self, network: Network) -> None:
        self.network = network
        self.show_object("", None)

    def show_object(self, kind: str, selected: Road | Node | BusStop | None) -> None:
        """Show the editor for a road, an intersection ("node"), an end marker ("entry"/"exit") or a
        bus stop ("bus_stop")."""
        if (kind, selected) != (self.kind, self.selected):
            self.drag_button.setChecked(False)  # drag mode belongs to one selection
        self.kind, self.selected = kind, selected
        nothing = selected is None or self.network is None
        self.empty_hint.setVisible(nothing)
        self.header.setVisible(not nothing)
        self.drag_button.setVisible(not nothing and kind != "bus_stop")  # a bus stop drags on its own
        self.delete_node_button.setVisible(isinstance(selected, Node))
        self.bus_stop_button.setVisible(isinstance(selected, Road) and not nothing)
        self.stop_fields.setVisible(isinstance(selected, BusStop) and not nothing)
        self.road_fields.setVisible(isinstance(selected, Road))
        self.node_fields.setVisible(kind == "node" and not nothing)
        self.signal_fields.setVisible(kind == "node" and not nothing and selected.control == "signalised")
        self.roundabout_fields.setVisible(kind == "node" and not nothing and selected.control == "roundabout")
        self.flow_fields.setVisible(kind in ("entry", "exit") and not nothing)
        self.multi_fields.hide()
        self.group_fields.hide()
        if nothing:
            return
        self.object_id.setText(selected.id)
        if isinstance(selected, Road):
            boundary = selected.id in self.network.approach_roads
            self.object_type.setText(("Boundary road" if boundary else "Road") + (" · single lane" if selected.single_lane else ""))
            self.length.setValue(selected.length_m)
            self.lanes.setValue(selected.lanes_per_direction)
            self.lane_changing.setChecked(selected.lane_changing)
            self.speed.setCurrentText(f"{selected.speed_limit_kmh:g}")
            self.direction.set_labels([
                "Two-way",
                f"One-way  {selected.from_node} → {selected.to_node}",
                f"One-way  {selected.to_node} → {selected.from_node}",
            ])
            self.direction.set_value(0 if selected.direction == "two-way" else 1)
            self.road_type.setCurrentText(selected.road_type)
            tightest = max((abs(kappa) for kappa in selected.curvature), default=0.0)
            self.curve_radius.setText(f"{1 / tightest:.1f} m radius" if tightest else "Straight")
            self._refresh_apply()
            a, b = selected.from_node, selected.to_node
            self.bus_stop_menu.clear()
            ways = [(f"{a} → {b}", "forward")]
            if selected.direction == "two-way":
                ways += [(f"{b} → {a}", "backward"), ("Both directions", "both")]
            for text, direction in ways:
                self.bus_stop_menu.addAction(text, lambda _=False, direction=direction: self.add_bus_stop(direction))
        elif isinstance(selected, BusStop):
            self._show_bus_stop(selected)
        elif kind == "node":
            where = "added off the grid" if selected.row is None else self.network.node_label(selected)
            self.object_type.setText(f"Intersection {where}")
            self.control.set_value(selected.control)
            self._show_signal(selected)
            if selected.control == "roundabout":
                design = design_roundabout(self.network, selected.id)
                info = self.roundabout_info
                info["type"].setText(f"{design.category} ({design.entry_lanes} entry lane"
                                     f"{'s' if design.entry_lanes > 1 else ''})")
                info["speed"].setText(f"{design.design_speed_kmh:g} km/h "
                                      f"(approach limit {design.approach_speed_kmh:g})")
                info["diameter"].setText(f"{design.inscribed_diameter_m:.1f} m")
                info["island"].setText(f"{design.island_diameter_m:.1f} m · "
                                       f"circulating road {design.circulatory_width_m:.1f} m")
                info["circulating"].setText(f"≈ {design.circulating_speed_kmh:.0f} km/h (fastest path)")
        elif kind == "entry":
            self.object_type.setText(f"End {selected.id}: entry into the network")
            self.flow_label.setText("Supply")
            self.flow.setValue(selected.supply_vph)
        else:
            self.object_type.setText(f"End {selected.id}: exit from the network")
            self.flow_label.setText("Demand")
            self.flow.setValue(selected.demand_vph)

    def show_multi(self, node_ids: list[str]) -> None:
        """Several junctions picked together: list them and offer matching interchanges."""
        self.show_object("", None)
        self.empty_hint.hide()
        self.header.show()
        self.object_type.setText(f"{len(node_ids)} junctions selected")
        self.object_id.setText(" · ".join(node_ids))
        count = len(node_ids)
        self.interchange_buttons["trumpet"].setVisible(count == 3)
        self.interchange_buttons["diamond"].setVisible(count == 4)
        self.interchange_buttons["spui"].setVisible(count == 4)
        self.multi_hint.setText(
            "Ctrl+click a junction to add or remove it." if count in (3, 4) else
            "Ctrl+click junctions to add or remove them: 3 make a trumpet, 4 a diamond or SPUI."
        )
        self.multi_fields.show()

    def show_group(self, objects: list[tuple[str, str]]) -> None:
        """A selection box's objects: how many of each, their IDs, and Delete Selected."""
        self.show_object("", None)
        self.empty_hint.hide()
        self.header.show()
        self.object_type.setText(describe_group(objects) + " selected")
        shown = [identifier for _, identifier in objects]
        self.object_id.setText(" · ".join(shown[:12]) + (f" and {len(shown) - 12} more" if len(shown) > 12 else ""))
        roads = [self.network.road(identifier) for kind, identifier in objects if kind == "road"]
        meet = len(roads) == len(objects) == 2 and len({roads[0].from_node, roads[0].to_node}
                                                         & {roads[1].from_node, roads[1].to_node}) == 1
        self._slip_roads = [road.id for road in roads]
        self.slip_road_button.setVisible(meet)
        self.group_fields.show()

    def _road_field_values(self) -> tuple:
        return (round(self.length.value(), 2), self.lanes.value(), self.lane_changing.isChecked(),
                self.speed.currentText().strip(), self.direction.value(),
                self.road_type.currentText().strip())

    @staticmethod
    def _road_values(road: Road) -> tuple:
        return (round(road.length_m, 2), road.lanes_per_direction, road.lane_changing,
                f"{road.speed_limit_kmh:g}", 0 if road.direction == "two-way" else 1, road.road_type)

    def _refresh_apply(self) -> None:
        if isinstance(self.selected, Road):
            self.apply_button.setEnabled(self._road_field_values() != self._road_values(self.selected))

    def apply_changes(self) -> None:
        if not isinstance(self.selected, Road) or self.network is None:
            return
        road_id = self.selected.id
        try:
            speed_limit = float(self.speed.currentText())
            if round(self.length.value(), 2) != round(self.selected.length_m, 2):
                # Only when edited: re-applying the 2-decimal display would nudge nodes.
                self.network.set_road_length(road_id, self.length.value())
            road = self.network.road(road_id)
            road.lanes_per_direction = self.lanes.value()
            road.lane_changing = self.lane_changing.isChecked()
            road.speed_limit_kmh = speed_limit
            # One-way traffic runs from_node → to_node, so the reverse option swaps them.
            road.direction = "two-way" if self.direction.value() == 0 else "one-way"
            if self.direction.value() == 2:
                self.network.reverse_road(road_id)  # a curve's points and curvature flip too
            road.road_type = self.road_type.currentText().strip() or "urban"
            road.single_lane = road.single_lane and road.direction == "one-way" and road.lanes_per_direction == 1
            self.network.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid road properties", str(exc))
            self.show_object("road", self.network.road(road_id))
            return
        self.network_changed.emit()

    def delete_road(self) -> None:
        if not isinstance(self.selected, Road) or self.network is None:
            return
        road_id = self.selected.id
        message = f"Delete road {road_id}?"
        if road_id in self.network.approach_roads:
            message += " Its boundary end and flows are removed too."
        if QMessageBox.question(self, "Delete road", message) != QMessageBox.StandardButton.Yes:
            return
        self.network.delete_road(road_id)
        self.show_object("", None)
        self.network_changed.emit()

    def delete_node(self) -> None:
        if not isinstance(self.selected, Node) or self.network is None:
            return
        node_id = self.selected.id
        roads = sum(node_id in (road.from_node, road.to_node) for road in self.network.all_roads())
        message = f"Delete {node_id} and its {roads} connecting road(s)?"
        if QMessageBox.question(self, "Delete node", message) != QMessageBox.StandardButton.Yes:
            return
        self.network.delete_node(node_id)
        self.show_object("", None)
        self.network_changed.emit()

    def add_bus_stop(self, direction: str) -> None:
        """A bus stop on the selected road, serving `direction` ("forward", "backward" or "both")."""
        if not isinstance(self.selected, Road) or self.network is None:
            return
        try:
            stop = self.network.add_bus_stop(self.selected.id, direction)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot add bus stop", str(exc))
            return
        self.bus_stop_changed.emit(stop.id)

    def delete_bus_stop(self) -> None:
        if isinstance(self.selected, BusStop) and self.network is not None:
            self.network.bus_stops.pop(self.selected.id, None)
            self.show_object("", None)
            self.network_changed.emit()

    def _show_bus_stop(self, stop: BusStop) -> None:
        """Fill the bus stop section; filling doesn't move it."""
        network = self.network
        road = network.road(stop.road)
        length = network.road_geometry_length(road)
        low, high = network.bus_stop_range(road)
        fits = low <= high
        self.object_type.setText(f"Bus stop on road {road.id}" +
                                 ("" if fits else " (too short for its bay now: runs leave it out)"))
        a, b = road.from_node, road.to_node
        self.stop_serves.set_labels([f"{a} → {b}", f"{b} → {a}", "Both directions"])
        one_way = road.direction == "one-way"  # serves the one direction there is
        for index, button in enumerate(self.stop_serves.group.buttons()):
            button.setEnabled(not one_way or index == 0)
        self.stop_serves.set_value("forward" if one_way else stop.direction)
        middle = network.bus_stop_distance(stop)
        self.stop_from_label.setText(f"From {a}")
        self.stop_to_label.setText(f"From {b}")
        for spin, value, bounds in ((self.stop_from, middle, (low, high)),
                                    (self.stop_to, length - middle, (length - high, length - low))):
            spin.blockSignals(True)
            spin.setRange(*(bounds if fits else (value, value)))
            spin.setValue(value)
            spin.setEnabled(fits)
            spin.blockSignals(False)
        self.stop_dwell.blockSignals(True)
        self.stop_dwell.setValue(stop.dwell_s)
        self.stop_dwell.blockSignals(False)
        for spin, value in ((self.stop_boarding, stop.boarding_per_h), (self.stop_alighting, stop.alighting_percent)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.stop_dwell.setEnabled(not (stop.boarding_per_h or stop.alighting_percent))  # passengers decide it then

    def apply_stop_from(self, metres: float) -> None:
        if isinstance(self.selected, BusStop):
            self.network.move_bus_stop(self.selected.id, metres)
            self.bus_stop_changed.emit(self.selected.id)

    def apply_stop_to(self, metres: float) -> None:
        if isinstance(self.selected, BusStop):
            road = self.network.road(self.selected.road)
            self.apply_stop_from(self.network.road_geometry_length(road) - metres)

    def apply_stop_dwell(self, seconds: float) -> None:
        if isinstance(self.selected, BusStop):
            self.selected.dwell_s = seconds
            self.bus_stop_changed.emit(self.selected.id)

    def apply_stop_passengers(self) -> None:
        if isinstance(self.selected, BusStop):
            self.selected.boarding_per_h = self.stop_boarding.value()
            self.selected.alighting_percent = self.stop_alighting.value()
            self.stop_dwell.setEnabled(not (self.selected.boarding_per_h or self.selected.alighting_percent))
            self.bus_stop_changed.emit(self.selected.id)

    def apply_stop_direction(self) -> None:
        if isinstance(self.selected, BusStop):
            self.selected.direction = self.stop_serves.value()
            self.bus_stop_changed.emit(self.selected.id)

    def _show_signal(self, node: Node) -> None:
        """Fill the signal section from the node; filling doesn't write back to it."""
        self.phases.set_value(node.signal_phases)
        self.timing_method.blockSignals(True)
        self.timing_method.setCurrentIndex(max(0, self.timing_method.findData(node.signal_method)))
        self.timing_method.blockSignals(False)
        for phase, (label, green) in enumerate(self.green_times):
            shown = phase < node.signal_phases
            label.setVisible(shown)
            green.setVisible(shown)
            if shown:
                green.blockSignals(True)
                green.setValue(node.green_times_s[phase])
                green.blockSignals(False)
            green.setEnabled(node.signal_method == "fixed")  # the other methods work them out
        method = node.signal_method
        for label, (widget, methods) in self.timing_settings.items():
            label.setVisible(method in methods)
            widget.setVisible(method in methods)
        for spin, value in ((self.cycle_min, node.cycle_min_s), (self.cycle_max, node.cycle_max_s),
                            (self.target_saturation, round(node.target_saturation * 100)),
                            (self.stop_weight, node.stop_weight_s)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.signal_note.setText(self._plan_note(method, self.run_plans.get(node.id)))

    @staticmethod
    def _plan_note(method: str, plan: dict | None) -> str:
        """What the run did with the junction's timing."""
        if method == "fixed":
            return "Runs use these greens every cycle."
        lead = {"webster": "Webster: each Run works out the cycle and greens from the OD demand (each phase's "
                           f"critical flow ratio, with {SATURATION_PER_LANE_VPH:,.0f} veh/h saturation flow per lane).",
                "transyt": "TRANSYT: each Run optimises the group's offsets and greens on one cycle.",
                "synchro": "Synchro: each Run picks the group's cycle and greens by percentile delay, then offsets.",
                "scats": "SCATS: adapts each cycle from its stop-line detectors.",
                "scoot": "SCOOT: adapts as it goes from its upstream detectors.",
                "actuated": "Actuated: each green extends while vehicles keep coming (3 s gap), 7 to 50 s.",
                "max_pressure": "Max pressure: after 7 s, the phase with most vehicles waiting goes next."}[method]
        if not plan or plan.get("method") != method:
            return lead
        greens = " / ".join(f"{green:g}" for green in plan["greens_s"])
        note = f"{lead} {'Now' if method in ('scats', 'scoot', 'actuated', 'max_pressure') else 'Last run'}: cycle {plan['cycle_s']:g} s, greens {greens} s"
        if method in ("webster", "actuated", "max_pressure"):
            if "Y" not in plan:
                return note + "."
            return note + f" (Y = {plan['Y']:g})."
        note += f", offset {plan['offset_s']:g} s, {plan['group']} junctions"
        if "pi_after" in plan:
            note += f"; delay and stops {plan['pi_before']:,} → {plan['pi_after']:,} veh·s/h"
        if "saturation" in plan:
            note += "; saturation " + " / ".join(f"{ds:.0%}" for ds in plan["saturation"])
        return note + "."

    def show_plans(self, plans: dict) -> None:
        """A run's signal plans (each frame's, while SCATS and SCOOT adapt): the junction's section
        shows what the run worked out."""
        selected = self.selected.id if isinstance(self.selected, Node) else None
        changed = plans.get(selected) != self.run_plans.get(selected)
        self.run_plans = plans
        if changed and isinstance(self.selected, Node) and self.selected.control == "signalised":
            self._show_signal(self.selected)

    def apply_phases(self) -> None:
        if isinstance(self.selected, Node):
            count = self.phases.value()
            self.selected.signal_phases = count
            # Keep entered greens; new phases start at 30 s.
            self.selected.green_times_s = (self.selected.green_times_s + [30.0] * count)[:count]
            self._show_signal(self.selected)
            self.edited.emit(("phases", self.selected.id))

    def apply_timing_method(self) -> None:
        if isinstance(self.selected, Node):
            self.selected.signal_method = self.timing_method.currentData()
            self._show_signal(self.selected)
            self.edited.emit(("method", self.selected.id))

    def apply_timing_setting(self, name: str, value: float) -> None:
        """Cycle limits, target saturation or stop weight; the limits keep their order."""
        if isinstance(self.selected, Node):
            setattr(self.selected, name, value)
            if self.selected.cycle_min_s > self.selected.cycle_max_s:
                other = "cycle_max_s" if name == "cycle_min_s" else "cycle_min_s"
                setattr(self.selected, other, value)
                self._show_signal(self.selected)
            self.edited.emit(("timing", self.selected.id, name))

    def apply_green(self, phase: int, seconds: float) -> None:
        if isinstance(self.selected, Node) and phase < len(self.selected.green_times_s):
            self.selected.green_times_s[phase] = seconds
            self.edited.emit(("green", self.selected.id, phase))

    def apply_control(self) -> None:
        if isinstance(self.selected, Node):
            node, before = self.selected, self.selected.control
            node.control = self.control.value()
            try:
                fit_slip_roads(self.network, node.id)  # slip roads past it clear of a roundabout's circle
            except ValueError as exc:
                node.control = before
                QMessageBox.warning(self, "Cannot change the junction", str(exc))
                self.show_object("node", node)
                return
            self.network_changed.emit()

    def apply_flow(self) -> None:
        if not isinstance(self.selected, Node):
            return
        if self.kind == "entry":
            self.selected.supply_vph = self.flow.value()
        else:
            self.selected.demand_vph = self.flow.value()
        self.network_changed.emit()
