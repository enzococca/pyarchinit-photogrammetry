"""Coloured icons for the interface (qtawesome: Material Design Icons), the application
logo and the colour palette of the workflow stages.

``icon("align")`` returns a :class:`QIcon`; without qtawesome it is an empty icon, so
the interface works (text only) in minimal installations.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon

# Stage colours: one hue per step of the workflow, reused in menus, toolbar and tree.
PALETTE = {
    "blue": "#2f80ed", "indigo": "#3f51b5", "violet": "#8e44ad", "teal": "#138d75", "green": "#27ae60",
    "lime": "#7cb342", "orange": "#e67e22", "amber": "#f39c12", "pink": "#e84393", "brown": "#8d6e63",
    "red": "#e74c3c", "olive": "#8d9a1a", "grey": "#6c7a89", "slate": "#34495e", "cyan": "#00acc1",
}

# name -> (qtawesome icon id, palette colour)
ICONS = {
    # file
    "new": ("mdi6.file-plus-outline", "blue"), "open": ("mdi6.folder-open-outline", "amber"),
    "recent": ("mdi6.history", "grey"), "save": ("mdi6.content-save-outline", "blue"),
    "save_as": ("mdi6.content-save-edit-outline", "blue"), "export": ("mdi6.export-variant", "slate"),
    "report": ("mdi6.file-chart-outline", "indigo"), "demo": ("mdi6.flask-outline", "violet"),
    "quit": ("mdi6.power", "grey"),
    # workflow
    "photos": ("mdi6.image-multiple-outline", "blue"), "folder_photos": ("mdi6.folder-image", "blue"),
    "chunk": ("mdi6.cube-outline", "slate"), "chunk_add": ("mdi6.cube-send", "slate"),
    "markers": ("mdi6.qrcode-scan", "violet"), "align": ("mdi6.vector-triangle", "teal"),
    "reference": ("mdi6.map-marker-radius-outline", "indigo"), "georef": ("mdi6.earth", "indigo"),
    "optimize": ("mdi6.tune-vertical", "teal"), "dense": ("mdi6.dots-hexagon", "green"),
    "model": ("mdi6.hexagon-multiple-outline", "orange"), "texture": ("mdi6.texture-box", "pink"),
    "dem": ("mdi6.terrain", "brown"), "ortho": ("mdi6.map-outline", "amber"),
    "process_all": ("mdi6.play-circle-outline", "green"), "cancel": ("mdi6.close-circle-outline", "red"),
    # tools
    "print": ("mdi6.printer-outline", "grey"), "turntable": ("mdi6.rotate-3d-variant", "grey"),
    "pan": ("mdi6.cursor-move", "grey"), "polygon": ("mdi6.vector-polygon", "red"),
    "line": ("mdi6.vector-line", "indigo"), "measure": ("mdi6.ruler", "teal"),
    "point": ("mdi6.map-marker-plus-outline", "green"), "eraser": ("mdi6.eraser", "grey"),
    "select": ("mdi6.select-drag", "indigo"), "delete": ("mdi6.delete-outline", "red"),
    "merge": ("mdi6.vector-union", "violet"), "freehand": ("mdi6.draw", "red"), "snap": ("mdi6.magnet", "violet"),
    "contours": ("mdi6.waves", "brown"), "volume": ("mdi6.shovel", "brown"),
    "slice": ("mdi6.layers-outline", "orange"), "pottery": ("mdi6.pot-outline", "orange"),
    "classify": ("mdi6.tag-multiple-outline", "lime"), "sam": ("mdi6.auto-fix", "violet"),
    "ifc": ("mdi6.office-building-outline", "slate"), "script": ("mdi6.language-python", "blue"),
    # PyArchInit
    "database": ("mdi6.database-outline", "olive"), "publish": ("mdi6.database-arrow-up-outline", "olive"),
    "load": ("mdi6.database-arrow-down-outline", "olive"), "refresh": ("mdi6.refresh", "grey"),
    "demo_db": ("mdi6.database-check-outline", "olive"),
    # views
    "view3d": ("mdi6.rotate-3d", "orange"), "view_ortho": ("mdi6.map", "amber"),
    "view_photo": ("mdi6.image-outline", "blue"), "console": ("mdi6.console", "slate"),
    "log": ("mdi6.text-box-outline", "grey"), "theme": ("mdi6.theme-light-dark", "grey"),
    "top_view": ("mdi6.arrow-collapse-down", "grey"), "home_view": ("mdi6.home-outline", "grey"),
    "workspace": ("mdi6.file-tree-outline", "slate"), "reference_pane": ("mdi6.table", "indigo"),
    # help and status
    "help": ("mdi6.help-circle-outline", "blue"), "about": ("mdi6.information-outline", "blue"),
    "doctor": ("mdi6.stethoscope", "teal"), "error": ("mdi6.alert-circle", "red"),
    "warning": ("mdi6.alert", "amber"), "info": ("mdi6.information", "blue"),
    "copy": ("mdi6.content-copy", "grey"), "folder": ("mdi6.folder-outline", "amber"),
    "search": ("mdi6.magnify", "grey"), "clear": ("mdi6.delete-sweep-outline", "grey"),
    "ok": ("mdi6.check-circle", "green"), "view": ("mdi6.eye-outline", "blue"), "edit": ("mdi6.pencil-outline", "slate"),
    "version": ("mdi6.layers-outline", "amber"),
    "previous": ("mdi6.chevron-left-circle-outline", "blue"), "next": ("mdi6.chevron-right-circle-outline", "blue"),
    "filter": ("mdi6.filter-outline", "blue"), "update": ("mdi6.cloud-download-outline", "green"),
    # tree items
    "camera_ok": ("mdi6.camera", "green"), "camera_bad": ("mdi6.camera-off", "red"),
    "cameras": ("mdi6.camera-burst", "blue"), "marker": ("mdi6.map-marker", "violet"),
    "gcp": ("mdi6.map-marker-check", "green"), "checkpoint": ("mdi6.map-marker-question", "amber"),
    "crs": ("mdi6.earth", "indigo"), "scalebar": ("mdi6.ruler", "teal"), "project": ("mdi6.briefcase-outline", "slate"),
    "site": ("mdi6.map-marker-outline", "olive"), "us": ("mdi6.layers-triple-outline", "brown"),
    "doc": ("mdi6.file-document-outline", "grey"),
    "tie_points": ("mdi6.dots-grid", "teal"), "point_cloud": ("mdi6.dots-hexagon", "green"),
    "model_textured": ("mdi6.texture-box", "pink"), "orthomosaic": ("mdi6.map-outline", "amber"),
    "hillshade": ("mdi6.weather-sunny", "amber"),
    # cleaning and cropping
    "crop": ("mdi6.crop", "cyan"), "patch": ("mdi6.image-edit-outline", "pink"),
    "patch_update": ("mdi6.image-refresh-outline", "pink"), "patch_remove": ("mdi6.image-remove-outline", "pink"),
    "confidence": ("mdi6.chart-bell-curve-cumulative", "teal"),
    "clean": ("mdi6.broom", "green"), "tie_clean": ("mdi6.filter-variant-remove", "teal"),
    "relink": ("mdi6.link-variant", "blue"),
}


def available() -> bool:
    if os.environ.get("PPG_NO_ICONS"):
        return False
    try:
        import qtawesome  # noqa: F401

        return True
    except Exception:
        return False


@lru_cache(maxsize=None)
def icon(name: str, color: str | None = None) -> QIcon:
    """Icon by logical name (see :data:`ICONS`), optionally with another palette colour."""
    spec = ICONS.get(name)
    if spec is None or not available():
        return QIcon()
    ident, col = spec
    col = PALETTE.get(color or col, color or PALETTE[col])
    try:
        import qtawesome as qta

        return qta.icon(ident, color=col, color_active=col, color_disabled="#b0b7bf")
    except Exception:
        return QIcon()


def color(name: str) -> str:
    """Hex colour of an icon name (for labels that match the icon)."""
    spec = ICONS.get(name)
    return PALETTE[spec[1]] if spec else PALETTE["grey"]


def resources_dir() -> Path:
    return Path(__file__).with_name("resources")


@lru_cache(maxsize=None)
def app_icon() -> QIcon:
    """The application logo (SVG in ``gui/resources``)."""
    svg = resources_dir() / "logo.svg"
    return QIcon(str(svg)) if svg.exists() else QIcon()


__all__ = ["icon", "color", "app_icon", "available", "ICONS", "PALETTE"]
