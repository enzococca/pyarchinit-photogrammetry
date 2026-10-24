"""Light and dark themes: Fusion style, palette and a style sheet shared by all widgets."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

LIGHT = {
    "window": "#f3f4f6", "panel": "#ffffff", "panel_alt": "#f8f9fb", "border": "#d8dce2", "text": "#1f2933",
    "text_dim": "#6b7280", "accent": "#2f80ed", "accent_text": "#ffffff", "hover": "#e8f0fe",
    "selected": "#d6e6ff", "toolbar": "#ffffff", "dock_title": "#e9ecf1", "tab": "#eef0f3",
    "hint_bg": "#fff8e1", "hint_border": "#f6c343", "error_bg": "#fdecea", "warn_bg": "#fff4e5",
    "code_bg": "#f6f8fa", "scroll": "#c5cad2", "progress": "#2f80ed",
}
DARK = {
    "window": "#1e2227", "panel": "#262b31", "panel_alt": "#2b3138", "border": "#3a4048", "text": "#e5e7eb",
    "text_dim": "#9aa3ad", "accent": "#4ea1ff", "accent_text": "#0b1b2b", "hover": "#30384a",
    "selected": "#2d4a73", "toolbar": "#262b31", "dock_title": "#2f353c", "tab": "#2b3138",
    "hint_bg": "#3b3520", "hint_border": "#c9a227", "error_bg": "#4a2a2a", "warn_bg": "#4a3a22",
    "code_bg": "#1b1f24", "scroll": "#4a525c", "progress": "#4ea1ff",
}

_RESOURCES = Path(__file__).with_name("resources")
_QSS = """
QMainWindow, QDialog, QWidget#startPage {{ background: {window}; }}
QWidget {{ color: {text}; font-size: 13px; }}
QToolBar {{ background: {toolbar}; border: none; border-bottom: 1px solid {border}; padding: 4px 6px; spacing: 2px; }}
QToolBar::separator {{ width: 1px; background: {border}; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 8px;
               color: {text}; }}
QToolButton:hover {{ background: {hover}; border-color: {border}; }}
QToolButton:pressed, QToolButton:checked {{ background: {selected}; border-color: {accent}; }}
QToolButton:disabled {{ color: {text_dim}; }}
QToolButton#qt_toolbar_ext_button {{ padding: 0px; border-radius: 4px; }}  /* the >> of a full toolbar */
QMenuBar {{ background: {toolbar}; border-bottom: 1px solid {border}; padding: 2px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {hover}; }}
QMenu {{ background: {panel}; border: 1px solid {border}; padding: 6px; }}
QMenu::item {{ padding: 6px 28px 6px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {hover}; }}
QMenu::item:disabled {{ color: {text_dim}; }}  /* also the panes always there in Vista > Pannelli */
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}
QMenu::icon {{ margin-left: 6px; }}
QDockWidget {{ titlebar-close-icon: url("{dock_close}"); titlebar-normal-icon: url("{dock_float}"); }}
QDockWidget::title {{ background: {dock_title}; padding: 6px 10px; border: 1px solid {border}; border-bottom: none;
                      font-weight: 600; text-align: left; }}
QDockWidget::close-button, QDockWidget::float-button {{ background: transparent; border: none; border-radius: 4px;
                                                       icon-size: 22px; }}  /* Qt draws 5/8 of it: 13 px */
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{ background: {hover}; }}
QDockWidget::close-button:pressed, QDockWidget::float-button:pressed {{ background: {selected}; }}
QDockWidget > QScrollArea {{ border: 1px solid {border}; }}
/* the tabs of the panes; the arrows of a row of tabs too long for its side */
QMainWindow > QTabBar::tab, QDockWidgetGroupWindow > QTabBar::tab {{ padding: 5px 7px; margin-right: 1px; }}
QTabBar::scroller {{ width: 28px; }}
QTabBar QToolButton {{ background: {panel}; border: 1px solid {border}; border-radius: 4px; padding: 0px; margin: 0px;
                       width: 12px; }}
QTabBar QToolButton:hover {{ background: {hover}; }}
QTabWidget::pane {{ border: 1px solid {border}; background: {panel}; top: -1px; }}
QTabBar::tab {{ background: {tab}; color: {text_dim}; padding: 7px 14px; border: 1px solid {border};
                border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {panel}; color: {text}; border-bottom: 2px solid {accent}; }}
QTabBar::tab:hover:!selected {{ background: {hover}; }}
QTreeWidget, QTreeView, QListWidget, QTableWidget, QPlainTextEdit, QTextEdit, QGraphicsView {{
    background: {panel}; alternate-background-color: {panel_alt}; border: 1px solid {border}; border-radius: 4px;
    selection-background-color: {selected}; selection-color: {text}; }}
QTreeWidget::item, QListWidget::item {{ padding: 3px 2px; }}
QHeaderView::section {{ background: {panel_alt}; color: {text_dim}; padding: 5px 6px; border: none;
                        border-right: 1px solid {border}; border-bottom: 1px solid {border}; font-weight: 600; }}
QPushButton {{ background: {panel}; border: 1px solid {border}; border-radius: 6px; padding: 5px 14px; min-height: 18px; }}
QPushButton:hover {{ background: {hover}; border-color: {accent}; }}
QPushButton:pressed {{ background: {selected}; }}
QPushButton:default {{ background: {accent}; color: {accent_text}; border-color: {accent}; font-weight: 600; }}
QPushButton:default:hover {{ background: {accent}; }}
QPushButton:disabled {{ color: {text_dim}; background: {panel_alt}; }}
QPushButton#primary {{ background: {accent}; color: {accent_text}; border-color: {accent}; font-weight: 600;
                        padding: 9px 18px; }}
QPushButton#card {{ text-align: left; padding: 12px 16px; font-size: 14px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {panel}; border: 1px solid {border}; border-radius: 5px;
    padding: 4px 6px; min-height: 20px; selection-background-color: {accent}; selection-color: {accent_text}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {panel}; border: 1px solid {border}; selection-background-color: {selected}; }}
QCheckBox {{ spacing: 6px; }}
QStatusBar {{ background: {toolbar}; border-top: 1px solid {border}; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ border: 1px solid {border}; border-radius: 5px; background: {panel_alt}; text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {progress}; border-radius: 4px; }}
QSplitter::handle {{ background: {border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {scroll}; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {panel}; color: {text}; border: 1px solid {border}; padding: 4px 6px; }}
QLabel#hintBox {{ background: {hint_bg}; border: 1px solid {hint_border}; border-radius: 6px; padding: 8px 10px; }}
QLabel#errorSummary {{ font-size: 14px; font-weight: 600; }}
QLabel#pageTitle {{ font-size: 24px; font-weight: 700; }}
QLabel#pageSubtitle {{ font-size: 14px; color: {text_dim}; }}
QLabel#sectionTitle {{ font-size: 12px; font-weight: 700; color: {text_dim}; letter-spacing: 1px; }}
QPlainTextEdit#tracebackBox, QPlainTextEdit#consoleOut {{ background: {code_bg}; font-family: Menlo, Consolas, monospace; }}
QFrame#card {{ background: {panel}; border: 1px solid {border}; border-radius: 10px; }}
QTableWidget#logTable {{ gridline-color: {border}; }}
"""


def apply(app: QApplication, mode: str = "light") -> None:
    """Apply the theme (``light`` or ``dark``) to the application."""
    tokens = DARK if mode == "dark" else LIGHT
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: tokens["window"], QPalette.ColorRole.WindowText: tokens["text"],
        QPalette.ColorRole.Base: tokens["panel"], QPalette.ColorRole.AlternateBase: tokens["panel_alt"],
        QPalette.ColorRole.Text: tokens["text"], QPalette.ColorRole.Button: tokens["panel"],
        QPalette.ColorRole.ButtonText: tokens["text"], QPalette.ColorRole.Highlight: tokens["accent"],
        QPalette.ColorRole.HighlightedText: tokens["accent_text"], QPalette.ColorRole.ToolTipBase: tokens["panel"],
        QPalette.ColorRole.ToolTipText: tokens["text"], QPalette.ColorRole.PlaceholderText: tokens["text_dim"],
        QPalette.ColorRole.Link: tokens["accent"], QPalette.ColorRole.Mid: tokens["border"],
    }
    for role, col in roles.items():
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(tokens["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(tokens["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(tokens["text_dim"]))
    app.setPalette(pal)
    shade = "dark" if mode == "dark" else "light"  # the float and close buttons of the panes, in the theme's grey
    buttons = {f"dock_{kind}": (_RESOURCES / f"dock-{kind}-{shade}.svg").as_posix() for kind in ("close", "float")}
    app.setStyleSheet(_QSS.format(**tokens, **buttons))
    app.setProperty("ppg_theme", mode)


def current(app: QApplication | None = None) -> str:
    app = app or QApplication.instance()
    return (app.property("ppg_theme") if app is not None else None) or "light"


def tokens(mode: str | None = None) -> dict:
    return DARK if (mode or current()) == "dark" else LIGHT


def is_dark() -> bool:
    return current() == "dark"


__all__ = ["apply", "current", "tokens", "is_dark", "LIGHT", "DARK"]
