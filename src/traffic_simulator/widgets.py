"""The app's shared widgets: icon buttons, styled buttons, hints, collapsible sections, tabs, radio choices and a
dropdown of ticks. Qt; colours from theme.py, no model objects."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .theme import TEXT

# Icons are glyphs from Windows' own icon font: Segoe Fluent Icons on Windows 11, Segoe MDL2
# Assets on Windows 10, same code points.
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


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


def icon_button(glyph: int, name: str, role: str = "", checkable: bool = False) -> QPushButton:
    """A small button showing one icon glyph, its name as the tooltip (and for screen readers): a few
    words, never an explanation. Without an icon font it shows its name."""
    button = styled_button("", role, checkable=checkable)
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
        self.header = styled_button(title, "section", tooltip, checkable=True)
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



def styled_button(text: str, role: str = "", tooltip: str = "", checkable: bool = False) -> QPushButton:
    """Button tagged for the app stylesheet: role "primary" (one per area) or "danger"; `checkable`: a toggle."""
    button = QPushButton(text)
    button.setCheckable(checkable)
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
        self._checked = 0  # re-clicking the checked option is no change
        self.group.idClicked.connect(self._clicked)

    def _clicked(self, index: int) -> None:
        if index != self._checked:
            self._checked = index
            self.changed.emit()

    def value(self) -> object:
        return self.values[self.group.checkedId()]

    def set_value(self, value: object) -> None:
        self._checked = self.values.index(value)
        self.group.button(self._checked).setChecked(True)

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
