"""The cylindrical development in the window: the axis (vertical, the region's, the crest's, two points in the 3D
view) and the scale on the wall chosen in the elevation dialog, the tilt shown before building, the engine's refusals
shown, the tilt of an entry in the workspace tooltip."""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytest
from shapely.geometry import Point

from elevation_helpers import ORIGIN

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, processed, tmp_path):
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    src, _ch, _ = processed
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(src.path, d / src.path.name)
    shutil.copytree(src.files_dir, d / src.files_dir.name)
    (d / "photos").symlink_to(src.path.parent / "photos")
    doc = ppg.Document.open(d / src.path.name)
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


TILT = {"method": "crest", "tilt_deg": 6.6, "azimuth_deg": 46.0, "residual_m": 0.08, "centre_z": 101.0}


@pytest.fixture
def dialog(win, monkeypatch):
    """The window with a polygon selected around a "tower", the dialog answered by ``answers`` (a dict the test
    fills), the engine's cylinder recorded, the build run at once with the engine's build_elevation recorded."""
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.gui import main_window as mw

    rec = {"answers": {}, "circles": [], "builds": [], "choices": [], "warnings": [], "fields": []}
    poly = Point(ORIGIN[0] + 2.0, ORIGIN[1] + 1.5).buffer(0.5)
    monkeypatch.setattr(win, "_plan_selection", lambda: poly)

    def ask(parent, title, fields, intro=""):
        rec["fields"].append(fields)
        return {"source": ("cylinder", None), "inside": False, "front": 0.5, "back": 0.5, "resolution": 0.05,
                "blending": "mosaic", "colour": False, "name": "", **rec["answers"]}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    real = E.cylinder_from_circle

    def circle(ch, ring, inside=True, axis="vertical", axis_points=None, depth=None):
        rec["circles"].append({"axis": axis, "axis_points": axis_points, "depth": depth, "inside": inside})
        if rec["answers"].get("real"):
            return real(ch, ring, inside, axis=axis, axis_points=axis_points, depth=depth)
        c = real(ch, ring, inside)
        if axis != "vertical":
            c = E.Cylinder(c.centre, [0.08, 0.08, 1.0], c.radius, c.inside, c.start, tilt=TILT)
        return c

    monkeypatch.setattr(E, "cylinder_from_circle", circle)
    monkeypatch.setattr(E, "build_elevation", lambda *a, **k: rec["builds"].append((a, k)) or {})
    monkeypatch.setattr(win, "run_task", lambda title, fn, done=None, **k: fn(None))

    def choose(parent, kind, title, text, options, default=0, cancel="Annulla"):
        rec["choices"].append(text)
        return rec.get("choice", 0)

    monkeypatch.setattr(mw, "choice", choose)
    monkeypatch.setattr(win, "_warn", lambda text: rec["warnings"].append(text))
    monkeypatch.setattr(win, "_exc", lambda prefix, exc: rec["warnings"].append(f"{prefix}: {exc}"))
    return win, rec


def test_axis_choices_and_region_only_with_a_region(win):
    from pyarchinit_photogrammetry.engine.region import Region, set_region

    fields = {f.name: f for f in win._elevation_fields("cylinder")}
    assert "wall_scale" in fields and fields["wall_scale"].default is False
    values = [c[0] for c in fields["axis"].choices]
    assert values == ["vertical", "crest", "points"] and fields["axis"].default == "vertical"
    set_region(win.chunk, Region(ORIGIN + [2, 1.5, 0], [3, 3, 3], np.eye(3)))
    values = [c[0] for c in {f.name: f for f in win._elevation_fields("cylinder")}["axis"].choices]
    assert values == ["vertical", "region", "crest", "points"]


def test_the_choices_reach_the_engine_and_the_tilt_is_shown_first(dialog):
    win, rec = dialog
    rec["answers"].update(axis="crest", wall_scale=True)
    rec["choice"] = None  # seen the tilt, not built
    win.build_elevation()
    assert rec["circles"][-1]["axis"] == "crest" and rec["circles"][-1]["inside"] is False
    lo, hi = rec["circles"][-1]["depth"]
    assert lo <= -0.5 and hi >= 0.5
    assert "6,6°" in rec["choices"][-1] and "NE" in rec["choices"][-1]
    assert not rec["builds"]
    rec["choice"] = 0
    win.build_elevation()
    (_args, kwargs), = rec["builds"]
    assert kwargs["scale"] == "wall" and _args[1].tilt == TILT
    # the vertical axis: no question, the scale of the cylinder
    rec["answers"].update(axis="vertical", wall_scale=False)
    n = len(rec["choices"])
    win.build_elevation()
    assert len(rec["choices"]) == n and rec["builds"][-1][1]["scale"] == "cylinder"
    assert rec["circles"][-1]["axis"] == "vertical"


def test_two_points_picked_in_the_3d_view(dialog, monkeypatch):
    win, rec = dialog
    picks = []
    monkeypatch.setattr(win.model_view, "start_pick", lambda purpose="marker": picks.append(purpose))
    rec["answers"].update(axis="points")
    win.build_elevation()
    assert picks == ["axis_p"] and not rec["circles"]
    ch = win.chunk
    world = np.array([ORIGIN + [2.0, 1.5, -0.5], ORIGIN + [2.1, 1.5, 1.5]])
    win.model_view.elevation_requested.emit("axis", list(ch.to_internal(world)))
    got = np.asarray(rec["circles"][-1]["axis_points"])
    assert rec["circles"][-1]["axis"] == "points" and np.allclose(got, world, atol=1e-6)
    assert rec["builds"]


def test_the_engine_refusal_is_shown(dialog, monkeypatch):
    win, rec = dialog
    monkeypatch.setattr(win.model_view, "start_pick", lambda purpose="marker": None)
    rec["answers"].update(axis="points", real=True)
    win.build_elevation()
    ch = win.chunk
    world = np.array([ORIGIN + [2.0, 1.5, 0.0], ORIGIN + [2.0, 1.5, 0.3]])
    win.model_view.elevation_requested.emit("axis", list(ch.to_internal(world)))
    assert not rec["builds"] and rec["warnings"] and "1 m" in rec["warnings"][-1]


def test_the_tilt_in_the_workspace_tooltip(win):
    from PySide6.QtCore import Qt

    win.chunk.meta.setdefault("elevations", []).append(
        {"id": "abc", "name": "Torre", "size": [10, 10], "resolution": 0.01, "frame": {}, "tilt": TILT,
         "scale": "wall"})
    win.workspace.populate(win.doc)
    tips = []

    def walk(item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "elevation":
            tips.append(item.toolTip(0))
        for i in range(item.childCount()):
            walk(item.child(i))

    tree = win.workspace
    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    tip = next(t for t in tips if "inclinato" in t)
    assert "6,6°" in tip and "NE" in tip and "parete" in tip


def test_points_off_the_centre_are_warned_and_esc_gives_up(dialog, monkeypatch):
    import pyarchinit_photogrammetry.gui.main_window  # noqa: F401
    from pyarchinit_photogrammetry.engine import elevation as E

    win, rec = dialog
    monkeypatch.setattr(win.model_view, "start_pick", lambda purpose="marker": None)
    real = E.cylinder_from_circle

    def off_centre(ch, ring, inside=True, axis="vertical", axis_points=None, depth=None):
        c = real(ch, ring, inside)
        return E.Cylinder(c.centre, [0.05, 0.0, 1.0], c.radius, c.inside, c.start,
                          tilt={**TILT, "method": "points", "residual_m": None, "points_drift_m": 0.42,
                                "points_angle_deg": 7.6})

    monkeypatch.setattr(E, "cylinder_from_circle", off_centre)
    rec["answers"].update(axis="points")
    rec["choice"] = None
    world = np.array([ORIGIN + [2.0, 1.5, -0.5], ORIGIN + [2.1, 1.5, 1.5]])
    win.build_elevation()
    win.model_view.elevation_requested.emit("axis", list(win.chunk.to_internal(world)))
    assert "non è parallela alla torre" in rec["choices"][-1] and "7,6°" in rec["choices"][-1]
    assert "42 cm" in rec["choices"][-1]
    rec["answers"].update(axis="points")  # too little wall to check: said, not warned
    monkeypatch.setattr(E, "cylinder_from_circle", lambda ch, ring, inside=True, **k: E.Cylinder(
        real(ch, ring, inside).centre, [0.05, 0.0, 1.0], 1.0, inside,
        tilt={**TILT, "method": "points", "residual_m": None, "points_drift_m": None, "points_angle_deg": None}))
    win.build_elevation()
    win.model_view.elevation_requested.emit("axis", list(win.chunk.to_internal(world)))
    assert "Non si può verificare" in rec["choices"][-1] and "non è parallela" not in rec["choices"][-1]
    monkeypatch.setattr(E, "cylinder_from_circle", off_centre)
    assert not rec["builds"]
    # Esc during the picks: the points that come later build nothing
    win.build_elevation()
    win.model_view.elevation_requested.emit("axis_cancel", None)
    n = len(rec["choices"])
    win.model_view.elevation_requested.emit("axis", list(win.chunk.to_internal(world)))
    assert len(rec["choices"]) == n and not rec["builds"]


def test_esc_in_the_3d_view_gives_up_the_axis(win):
    got = []
    win.model_view.elevation_requested.connect(lambda kind, data: got.append(kind))
    win._axis_pending = {"v": {}, "ring": None, "chunk": win.chunk.key}
    win.model_view._purpose = "axis_p2"
    win.model_view._pick_finished(None)
    assert got == ["axis_cancel"] and win._axis_pending is None
