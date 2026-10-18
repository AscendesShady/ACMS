"""The app's desktop workspace: matte slate panels, blue actions and clear keyboard focus.
Small floating toolbars retain a restrained glass finish; data and form surfaces stay opaque.

Applied to the whole application, so dialogs, menus and tooltips match. Every text/background pair in
READABLE_PAIRS keeps WCAG contrast of at least 4.5:1 (tests/test_theme.py checks them); a glass fill is checked as
the solid colour it makes over the backdrop (`over`).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QGraphicsDropShadowEffect, QWidget


def over(base: str, alpha: float, tint: str = "#ffffff") -> str:
    """The solid colour `tint` at `alpha` makes over `base` (a glass fill as seen on the backdrop)."""
    channels = [round(int(base[i:i + 2], 16) * (1 - alpha) + int(tint[i:i + 2], 16) * alpha) for i in (1, 3, 5)]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


WINDOW = "#111827"  # the workspace behind the panels; never pure black
PANEL = "#1b2433"  # a quiet, opaque sidebar, distinct from the canvas and input boxes
BACKDROP_TOP, BACKDROP_BOTTOM = WINDOW, WINDOW  # retained for callers using the backdrop tokens
SURFACE = "#151e2c"  # inputs, tables and lists
BUTTON, BUTTON_HOVER = "#273449", "#33455e"
BORDER, DIVIDER = "#42536d", "#303e53"
# Glass: white at these strengths over what lies under it (the package's dark variant: 8 %), its rim lit at 135°
GLASS_ALPHA, GLASS_HOVER_ALPHA, GLASS_SELECTED_ALPHA = 0.08, 0.14, 0.20
GLASS = f"rgba(255, 255, 255, {GLASS_ALPHA})"
GLASS_HOVER = f"rgba(255, 255, 255, {GLASS_HOVER_ALPHA})"
GLASS_SELECTED = f"rgba(255, 255, 255, {GLASS_SELECTED_ALPHA})"
GLASS_SHEEN = ("qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1, stop: 0 rgba(255, 255, 255, 0.14), "
               "stop: 0.5 rgba(255, 255, 255, 0.07), stop: 1 rgba(255, 255, 255, 0.05))")  # the specular light
RIM_LIGHT, RIM_DARK = "rgba(255, 255, 255, 0.30)", "rgba(255, 255, 255, 0.08)"  # top-left edge, bottom-right edge
TEXT = "#f1f5f9"
SECONDARY_TEXT = "#b6c3d5"  # hints and explanations
DISABLED_TEXT = "#95a4b9"
ACCENT, ACCENT_HOVER = "#2563eb", "#1d4ed8"  # primary actions, active tools and selection
ACCENT_EDGE = "#8ab8ff"  # keyboard focus and selection outlines
BANNER = "#203d68"  # next-step banner, selected grid cell and navigation
PRESSED = "#254a82"  # held and briefly flashed buttons (ClickFlash)
TOOLTIP = "#202c40"
DANGER, DANGER_SURFACE = "#fca5a5", "#642b37"
SHADOW = "#0b101b"
RADIUS, RADIUS_SMALL, RADIUS_LARGE = 12, 6, 16
CAPSULE = 13  # retained for small floating toolbars
# Status text on the backdrop (test monitor): light tints keep contrast.
STATUS = {"running": "#93c5fd", "passed": "#86efac", "failed": "#fca5a5", "waiting": "#fcd34d"}

# Vehicle paint and non-text skin details. Type colours remain categorical in demand charts.
VEHICLE_PAINTS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767",
                  "#e2e8f0", "#64748b", "#c4aa7d", "#263241")
VEHICLE_INKS = ("#21394c", "#17222c", "#fff0bf", "#ee4148", "#438bff", "#e8edf0")
BUILDING_PAINTS = {"Residential": ("#c3aa8b", "#886354"), "School": ("#d7c8a2", "#657d7b"),
                   "Hospital": ("#c6d4d9", "#69858e"), "Office": ("#a2b0bc", "#495c6f"),
                   "Shop": ("#c8b294", "#597e68")}

READABLE_PAIRS = [
    (TEXT, WINDOW), (TEXT, SURFACE), (TEXT, BUTTON), (TEXT, over(WINDOW, GLASS_HOVER_ALPHA)),
    (TEXT, over(WINDOW, GLASS_SELECTED_ALPHA)), (TEXT, ACCENT), (TEXT, BANNER), (TEXT, TOOLTIP), (TEXT, PRESSED),
    (SECONDARY_TEXT, WINDOW), (SECONDARY_TEXT, SURFACE), (SECONDARY_TEXT, BUTTON), (DISABLED_TEXT, WINDOW),
    (TEXT, PANEL), (TEXT, BUTTON_HOVER), (TEXT, ACCENT_HOVER), (TEXT, DANGER_SURFACE),
    (SECONDARY_TEXT, PANEL), (SECONDARY_TEXT, BUTTON_HOVER), (DISABLED_TEXT, SURFACE),
    (DISABLED_TEXT, PANEL), (DISABLED_TEXT, BUTTON), (ACCENT_EDGE, PANEL), (DANGER, PANEL),
    *((colour, WINDOW) for colour in STATUS.values()), *((colour, SURFACE) for colour in STATUS.values()),
]

_GLASS_EDGES = (f"border: 1px solid {RIM_DARK}; border-top-color: {RIM_LIGHT}; "
                f"border-left-color: {RIM_LIGHT};")  # lit from the upper left

APP_STYLE = f"""
QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
QMainWindow, QDialog, QWidget#appShell {{ background: {WINDOW}; }}
/* Keep the native control geometry; use colour, not thicker borders, for focus and press feedback. */
QPushButton {{ min-height: 18px; padding: 3px 12px; border: 1px solid {BORDER};
               border-radius: {RADIUS_SMALL}px; background: {BUTTON}; color: {TEXT}; }}
QPushButton:hover {{ background: {BUTTON_HOVER}; border-color: {ACCENT_EDGE}; }}
QPushButton:focus {{ border: 1px solid {ACCENT_EDGE}; background: {BUTTON_HOVER}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT_EDGE}; color: {TEXT}; }}
QPushButton:disabled {{ background: {SURFACE}; border-color: {DIVIDER}; color: {DISABLED_TEXT}; }}
QPushButton[role="primary"] {{ background: {ACCENT}; border: 1px solid {ACCENT_EDGE}; color: {TEXT};
                               font-weight: 600; }}
QPushButton[role="primary"]:hover, QPushButton[role="primary"]:focus {{ background: {ACCENT_HOVER}; }}
QPushButton[role="primary"]:disabled {{ background: {SURFACE}; border-color: {DIVIDER}; color: {DISABLED_TEXT}; }}
QPushButton[role="danger"] {{ border-color: {DANGER}; }}
QPushButton[role="danger"]:hover {{ background: {DANGER_SURFACE}; }}
/* Icon and mini buttons retain their established hit areas and accessible labels. */
QPushButton[glyph="true"] {{ min-width: 18px; padding: 3px 2px; border-radius: {RADIUS_SMALL}px; }}
/* A mini icon button (beside a box it fills, e.g. Random): a small glyph, filled only when hovered, clickable over the full 26 px square. */
QPushButton[role="mini"] {{ min-height: 20px; min-width: 20px; padding: 3px; border: none; border-radius: {RADIUS_SMALL}px;
                            background: transparent; }}
QPushButton[role="mini"]:hover {{ background: {BUTTON_HOVER}; }}
QPushButton[role="mini"]:focus {{ border: 1px solid {ACCENT_EDGE}; }}
/* Content stays opaque: text boxes rounded, a focus ring in the accent. Spin boxes and drop-downs keep Fusion's
   own drawing in this palette (styled, Qt drops their arrows unless given images). */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: {RADIUS_SMALL}px;
    padding: 2px 6px; selection-background-color: {ACCENT}; selection-color: {TEXT}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {ACCENT_EDGE}; }}
QLineEdit:disabled {{ color: {DISABLED_TEXT}; }}
QComboBox QAbstractItemView {{ background: {TOOLTIP}; border: 1px solid {BORDER};
                               selection-background-color: {ACCENT}; outline: 0; }}
/* Fusion draws real checkmarks and radio dots, including their disabled and keyboard states. */
QCheckBox, QRadioButton {{ spacing: 6px; }}
/* Section titles lead each panel: semibold and white, with room above; what is inside
   the section stays at normal weight. */
QGroupBox {{ font-weight: 600; font-size: 10.5pt; margin-top: 16px; padding-top: 4px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 2px; padding: 0 2px; color: {TEXT}; }}
QGroupBox QWidget {{ font-weight: 400; font-size: 10pt; }}
/* A collapsible section's title (properties_panel.Collapsible): chevron and title on the left, a
   rule under it, no fill until hovered. */
QPushButton[role="section"] {{ text-align: left; font-weight: 600; font-size: 10.5pt; min-height: 30px;
                               padding: 4px 8px; border: none; border-bottom: 1px solid {DIVIDER};
                               border-radius: 0; background: transparent; }}
QPushButton[role="section"]:checked {{ background: transparent; }}
QPushButton[role="section"]:hover {{ background: {BUTTON}; border-radius: {RADIUS_SMALL}px; }}
QPushButton[role="section"]:focus {{ border: 1px solid {ACCENT_EDGE}; }}
QLabel[role="hint"] {{ color: {SECONDARY_TEXT}; }}
QLabel[role="brand"] {{ color: {ACCENT_EDGE}; font-size: 20px; font-weight: 700; }}
QLabel[role="pageTitle"] {{ color: {TEXT}; font-size: 14px; font-weight: 600; }}
QLabel[role="clock"] {{ color: {TEXT}; font-family: "Consolas"; font-size: 10pt; }}
QLabel[role="banner"] {{ background: {BANNER}; color: {TEXT}; border: 1px solid {ACCENT_EDGE};
                         border-radius: {RADIUS_SMALL}px; padding: 6px 10px; }}
/* Opaque panels keep form text readable; only the small floating bars use glass. */
QScrollArea#sidebar {{ background: {PANEL}; border: 1px solid {DIVIDER}; border-radius: {RADIUS}px; }}
QScrollArea#sidebar > QWidget > QWidget {{ background: transparent; }}
QHeaderView::section {{ background: {BUTTON}; color: {SECONDARY_TEXT}; border: none;
                        border-right: 1px solid {DIVIDER}; border-bottom: 1px solid {BORDER}; padding: 4px 6px; }}
QHeaderView::section:checked {{ background: {ACCENT}; color: {TEXT}; font-weight: 600; }}
QTableView, QTreeView, QListView {{ background: {SURFACE}; alternate-background-color: {PANEL};
    color: {TEXT}; border: 1px solid {BORDER}; border-radius: {RADIUS_SMALL}px;
    selection-background-color: {BANNER}; selection-color: {TEXT}; }}
QTableView {{ gridline-color: {DIVIDER}; }}
/* A clear selected navigation tile; each tab keeps the same dimensions in all states. */
QTabWidget::pane {{ border: none; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: {SURFACE}; color: {SECONDARY_TEXT}; border: 1px solid {DIVIDER};
                border-radius: {RADIUS_SMALL}px; padding: 6px 10px; margin: 2px; }}
QTabBar::tab:selected {{ background: {BANNER}; border-color: {ACCENT_EDGE}; color: {TEXT}; }}
QTabBar::tab:hover:!selected {{ background: {BUTTON_HOVER}; border-color: {BORDER}; color: {TEXT}; }}
QTabBar#iconTabs::tab {{ width: 20px; }}  /* the sidebar's icon-only tabs: no room kept for a name */
QProgressBar {{ background: {BUTTON}; color: {TEXT}; border: none; border-radius: 4px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QSlider::groove:horizontal {{ height: 4px; background: {GLASS_SELECTED}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -7px 0; border-radius: 9px; background: {TEXT};
                              border: 1px solid {RIM_LIGHT}; }}
/* Scroll bars: thin capsules that brighten on hover, no arrows. */
QScrollBar:vertical {{ width: 10px; background: transparent; margin: 2px; }}
QScrollBar:horizontal {{ height: 10px; background: transparent; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {SECONDARY_TEXT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {TOOLTIP}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
QMenu {{ background: {TOOLTIP}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 4px 18px; border-radius: {RADIUS_SMALL}px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {DIVIDER}; margin: 4px 8px; }}
/* Menu bar (File, Edit and the simulation controls): room above and below so it doesn't look
   stuck to the window's edge or to the panels under it. */
QMenuBar {{ padding: 5px 6px; background: {WINDOW}; color: {SECONDARY_TEXT}; }}
QMenuBar::item {{ padding: 3px 10px; border-radius: {RADIUS_SMALL}px; background: transparent; }}
QMenuBar::item:selected, QMenuBar::item:pressed {{ background: {BUTTON_HOVER}; color: {TEXT}; }}
QStatusBar {{ background: {WINDOW}; color: {SECONDARY_TEXT}; border-top: 1px solid {DIVIDER}; }}
QStatusBar::item {{ border: none; }}
/* Floating glass bars (the run controls, the map's corner): a capsule platter for their buttons. */
QToolBar#glassBar, QWidget#glassBar {{ background: {GLASS_SHEEN}; {_GLASS_EDGES} border-radius: {RADIUS}px;
                                       padding: 2px 6px; spacing: 4px; }}
QToolButton {{ border: 1px solid transparent; border-radius: {RADIUS_SMALL}px; padding: 3px; color: {TEXT}; }}
QToolButton:hover {{ background: {BUTTON_HOVER}; }}
QToolButton:focus {{ border-color: {ACCENT_EDGE}; background: {BUTTON_HOVER}; }}
QToolButton:checked {{ background: {ACCENT}; border-color: {ACCENT_EDGE}; }}
QPushButton:pressed, QToolButton:pressed, QPushButton[role="mini"]:pressed, QPushButton[role="section"]:pressed,
QPushButton[flash="true"], QToolButton[flash="true"], QPushButton[role="mini"][flash="true"] {{
    background: {PRESSED}; border: 1px solid {ACCENT_EDGE}; color: {TEXT}; }}
QPushButton[role="primary"]:pressed, QPushButton[role="primary"][flash="true"] {{ background: {BANNER}; border-color: {TEXT}; }}
QPushButton[role="mini"]:pressed, QPushButton[role="mini"][flash="true"] {{ border: none; }}
QPushButton[role="section"][flash="true"], QPushButton[role="section"]:checked:pressed {{ background: {PRESSED}; }}
"""
FLASH_MS = 250  # how long a clicked button stays lit, so a click is seen even when it is quick


def float_glass(widget: QWidget) -> QWidget:
    """A floating glass bar (objectName glassBar, the stylesheet's capsule platter) with its soft shadow under it
    (GlassShadow's elevation, made stronger for a dark backdrop). Only for small bars: an effect re-draws its widget
    off screen."""
    widget.setObjectName("glassBar")
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # a plain QWidget paints its stylesheet
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(18)
    shadow.setOffset(0, 3)
    shadow_colour = QColor(SHADOW)
    shadow_colour.setAlpha(110)
    shadow.setColor(shadow_colour)
    widget.setGraphicsEffect(shadow)
    return widget


class ClickFlash(QObject):
    """Every button lights up (PRESSED) for FLASH_MS after a click, as well as while it is held down, so the user
    can see each click was taken. Installed on the application by apply_theme."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.MouseButtonRelease and isinstance(watched, QAbstractButton) \
                and watched.isEnabled() and watched.rect().contains(event.position().toPoint()):
            _lit(watched, True)
            QTimer.singleShot(FLASH_MS, lambda button=watched: _lit(button, False))
        return False


def _lit(button: QAbstractButton, on: bool) -> None:
    try:
        button.setProperty("flash", on)
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()
    except RuntimeError:  # the click closed or deleted it
        pass


def apply_theme(app: QApplication) -> None:
    """Fusion style with an explicit palette, so the OS light/dark setting can't mix in."""
    app.setStyle("Fusion")
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, colour in {
        roles.Window: WINDOW, roles.WindowText: TEXT, roles.Base: SURFACE, roles.AlternateBase: over(SURFACE, 0.03),
        roles.Text: TEXT, roles.Button: BUTTON, roles.ButtonText: TEXT, roles.BrightText: TEXT,
        roles.PlaceholderText: DISABLED_TEXT, roles.ToolTipBase: TOOLTIP, roles.ToolTipText: TEXT,
        roles.Highlight: ACCENT, roles.HighlightedText: TEXT, roles.Link: ACCENT_EDGE,
        # Fusion draws frames and bevels from these.
        roles.Light: over(WINDOW, 0.22), roles.Midlight: over(WINDOW, 0.14), roles.Mid: over(WINDOW, 0.04),
        roles.Dark: WINDOW, roles.Shadow: SHADOW,
    }.items():
        palette.setColor(role, QColor(colour))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_TEXT))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)
    if getattr(app, "_click_flash", None) is None:  # once per application, however many windows
        app._click_flash = ClickFlash(app)
        app.installEventFilter(app._click_flash)
