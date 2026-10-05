"""The app's one look: grey surfaces with white text, whatever the Windows theme.

Applied to the whole application, so dialogs, menus and tooltips match. Every
text/background pair in READABLE_PAIRS keeps WCAG contrast of at least 4.5:1
(tests/test_theme.py checks them).
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

WINDOW = "#3b4048"  # window and panel background
SURFACE = "#33383f"  # inputs, tables, lists
BUTTON = "#4a5058"
TEXT = "#ffffff"
SECONDARY_TEXT = "#d1d5db"  # hints and explanations
DISABLED_TEXT = "#8f97a0"
ACCENT = "#2563eb"  # the one primary action, active tools, selection
ACCENT_EDGE = "#60a5fa"  # focus rings and selection outlines
BANNER = "#1e3a8a"  # next-step banner, selected grid cell
TOOLTIP = "#23272c"
# Status text on grey (test monitor): light tints keep contrast.
STATUS = {"running": "#93c5fd", "passed": "#86efac", "failed": "#fca5a5", "waiting": "#fcd34d"}

READABLE_PAIRS = [
    (TEXT, WINDOW), (TEXT, SURFACE), (TEXT, BUTTON), (TEXT, ACCENT), (TEXT, BANNER), (TEXT, TOOLTIP),
    (SECONDARY_TEXT, WINDOW), (SECONDARY_TEXT, SURFACE),
    *((colour, WINDOW) for colour in STATUS.values()), *((colour, SURFACE) for colour in STATUS.values()),
]

APP_STYLE = f"""
QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
QPushButton {{ min-height: 18px; padding: 3px 12px; border: 1px solid #6b7280; border-radius: 6px;
               background: {BUTTON}; color: {TEXT}; }}
QPushButton:hover {{ background: #565d66; border-color: #9ca3af; }}
QPushButton:pressed {{ background: #40464d; }}
QPushButton:focus {{ border: 2px solid {ACCENT_EDGE}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT_EDGE}; color: {TEXT}; }}
QPushButton:disabled {{ background: #41464d; border-color: #545a62; color: {DISABLED_TEXT}; }}
QPushButton[role="primary"] {{ background: {ACCENT}; border-color: {ACCENT_EDGE}; color: {TEXT};
                               font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #1d4ed8; }}
QPushButton[role="primary"]:disabled {{ background: #41464d; border-color: #545a62; color: {DISABLED_TEXT}; }}
QPushButton[role="danger"] {{ border-color: #f87171; }}
QPushButton[role="danger"]:hover {{ background: #7f1d1d; }}
/* Buttons stand as tall as the input boxes (26 px); an icon button is square. */
QPushButton[glyph="true"] {{ min-width: 18px; padding: 3px 2px; }}
/* A mini icon button (beside a box it fills, e.g. Random): a small glyph, filled only when hovered, clickable over the full 26 px square. */
QPushButton[role="mini"] {{ min-height: 20px; min-width: 20px; padding: 3px; border: none; border-radius: 4px; background: transparent; }}
QPushButton[role="mini"]:hover {{ background: {BUTTON}; }}
QPushButton[role="mini"]:focus {{ border: 1px solid {ACCENT_EDGE}; }}
/* Section titles lead each panel: semibold and white, with room above; what is inside
   the section stays at normal weight. */
QGroupBox {{ font-weight: 600; font-size: 10.5pt; margin-top: 16px; padding-top: 4px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 2px; padding: 0 2px; color: {TEXT}; }}
QGroupBox QWidget {{ font-weight: 400; font-size: 10pt; }}
/* A collapsible section's title (properties_panel.Collapsible): chevron and title on the left, a
   rule under it, no fill until hovered. */
QPushButton[role="section"] {{ text-align: left; font-weight: 600; font-size: 10.5pt; min-height: 30px;
                               padding: 4px 6px; border: none; border-bottom: 1px solid #50565e;
                               border-radius: 0; background: transparent; }}
QPushButton[role="section"]:checked {{ background: transparent; }}
QPushButton[role="section"]:hover {{ background: {BUTTON}; }}
QPushButton[role="section"]:focus {{ border: 1px solid {ACCENT_EDGE}; }}
QLabel[role="hint"] {{ color: {SECONDARY_TEXT}; }}
QLabel[role="banner"] {{ background: {BANNER}; color: {TEXT}; border: 1px solid {ACCENT_EDGE};
                         border-radius: 6px; padding: 6px 8px; }}
QHeaderView::section:checked {{ background: {ACCENT}; color: {TEXT}; font-weight: 600; }}
QTableView {{ gridline-color: #50565e; }}
/* Tabs: slim, so the sidebar's seven icon tabs fit its width; the open one joins its page, underlined. */
QTabBar::tab {{ background: {BUTTON}; color: {TEXT}; border: 1px solid #50565e; border-bottom: none;
                border-top-left-radius: 6px; border-top-right-radius: 6px; padding: 6px 10px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {WINDOW}; border-color: #6b7280; border-bottom: 2px solid {ACCENT_EDGE}; }}
QTabBar::tab:hover:!selected {{ background: #565d66; }}
QTabBar#iconTabs::tab {{ width: 20px; }}  /* the sidebar's icon-only tabs: no room kept for a name */
QProgressBar {{ background: {BUTTON}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QToolTip {{ background: {TOOLTIP}; color: {TEXT}; border: 1px solid #6b7280; }}
/* Menu bar (File, Edit and the simulation controls): room above and below so it doesn't look
   stuck to the window's edge or to the panels under it. */
QMenuBar {{ padding: 5px 6px; }}
QMenuBar::item {{ padding: 3px 8px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected, QMenuBar::item:pressed {{ background: #565d66; }}
"""


def apply_theme(app: QApplication) -> None:
    """Fusion style with an explicit grey palette, so the OS light/dark setting can't mix in."""
    app.setStyle("Fusion")
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, colour in {
        roles.Window: WINDOW, roles.WindowText: TEXT, roles.Base: SURFACE, roles.AlternateBase: "#383d44",
        roles.Text: TEXT, roles.Button: BUTTON, roles.ButtonText: TEXT, roles.BrightText: TEXT,
        roles.PlaceholderText: "#aab1b9", roles.ToolTipBase: TOOLTIP, roles.ToolTipText: TEXT,
        roles.Highlight: ACCENT, roles.HighlightedText: TEXT, roles.Link: ACCENT_EDGE,
        # Fusion draws frames and bevels from these.
        roles.Light: "#5b626b", roles.Midlight: "#50565e", roles.Mid: "#2f343a",
        roles.Dark: "#25292e", roles.Shadow: "#1b1e21",
    }.items():
        palette.setColor(role, QColor(colour))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_TEXT))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)
