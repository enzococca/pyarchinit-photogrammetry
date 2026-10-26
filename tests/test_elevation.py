"""Elevations: orthophoto and relief of the model on a plane (a wall) or a cylinder (a well)."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import elevation as E

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_frames_map_cells_to_the_world_and_back(rng):
    fr = E.Plane([10.0, 5.0, 100.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    assert np.allclose(fr.normal, [0, -1, 0])
    uvd = rng.uniform(-3, 3, (50, 3))
    assert np.allclose(fr.coords(fr.point(uvd[:, 0], uvd[:, 1], uvd[:, 2])), uvd)
    for inside in (True, False):
        cy = E.Cylinder([1.0, 2.0, 0.0], [0, 0, 1], 2.5, inside=inside, start=[1, 1, 0])
        uvd = np.column_stack([rng.uniform(-7, 7, 50), rng.uniform(-1, 1, 50), rng.uniform(-0.5, 0.5, 50)])
        assert np.allclose(cy.coords(cy.point(uvd[:, 0], uvd[:, 1], uvd[:, 2])), uvd)
        p0 = cy.point(np.zeros(1), np.zeros(1), np.zeros(1))[0]
        p1 = cy.point(np.zeros(1), np.zeros(1), np.full(1, 0.1))[0]
        radial = np.linalg.norm(p1[:2] - [1, 2]) - np.linalg.norm(p0[:2] - [1, 2])
        assert np.isclose(radial, -0.1 if inside else 0.1)  # d is towards the viewer
        # u runs to the right of the viewer: up x towards the viewer
        right = np.cross([0, 0, 1], cy.view(np.zeros(1), np.zeros(1))[0])
        step = cy.point(np.full(1, 0.01), np.zeros(1), np.zeros(1))[0] - p0
        assert step @ right > 0
    circle = [[1 + 2 * np.cos(a), 2 + 2 * np.sin(a)] for a in np.linspace(0, 5, 7)]
    cy = E.cylinder_from_circle(None, circle)
    assert np.allclose(cy.centre[:2], [1, 2]) and np.isclose(cy.radius, 2.0)
    assert E.frame_from_dict(cy.to_dict()).to_dict() == cy.to_dict()
    pts = [[0, 0, 0], [2, 0, 0], [0, 0, 1.5], [2, 0.01, 1.4]]
    pl = E.plane_from_points(pts, toward=[1, 5, 0])
    assert pl.normal[1] > 0.99 and abs(pl.v_axis[2]) > 0.99


def test_elevation_of_a_trench_wall(processed, tmp_path):
    """The south face of the trench (y = 0.9 m, from 0.35 m below the ground to the ground) seen
    from inside the trench: the relief is about zero on the face, the orthophoto covers it."""
    doc, ch = _copy(processed, tmp_path)
    a = ORIGIN[:2] + [1.05, 0.9]
    b = ORIGIN[:2] + [2.95, 0.9]
    frame = E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [2.0, 1.5])
    assert frame.normal[1] > 0.99  # looks south from inside the trench
    entry = E.build_elevation(ch, frame, resolution=0.004, depth=(-0.08, 0.08), max_angle=89.0)
    assert entry in ch.meta["elevations"] and entry["name"] == "Prospetto"
    ortho, relief = E.elevation_paths(ch, entry)
    with rasterio.open(relief) as src:
        d = src.read(1)
        t = src.transform
        assert src.tags()["ppg_frame"]
    rows, cols = np.nonzero(np.isfinite(d))
    v = t.f - (rows + 0.5) * abs(t.e)  # height of the cells
    face = (v > ORIGIN[2] - 0.30) & (v < ORIGIN[2] - 0.05)
    assert face.sum() > 2000
    assert np.median(np.abs(d[rows[face], cols[face]])) < 0.01
    with rasterio.open(ortho) as src:
        alpha = src.read(4)
    assert (alpha[rows[face], cols[face]] > 0).mean() > 0.5
    # a well: the trench seen from a vertical cylinder at its centre, from inside
    ring = [ORIGIN[:2] + [2 + 0.5 * np.cos(t), 1.5 + 0.5 * np.sin(t)] for t in np.linspace(0, 6, 8)]
    well = E.cylinder_from_circle(ch, ring, inside=True)
    e2 = E.build_elevation(ch, well, resolution=0.01, depth=(-2.0, 0.4), max_angle=89.0)
    with rasterio.open(E.elevation_paths(ch, e2)[1]) as src:
        assert np.isfinite(src.read(1)).mean() > 0.05
    assert len(E.elevations(ch)) == 2 and E.remove_elevation(ch, e2["id"]) and len(E.elevations(ch)) == 1


def test_interface_elevation_from_a_section(processed, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _copy(processed, tmp_path)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    section = {"label": "S1", "p1": list(ORIGIN[:2] + [1.05, 0.9]), "p2": list(ORIGIN[:2] + [2.95, 0.9])}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "side": "left", "front": 0.08, "back": 0.08, "resolution": 0.008, "blending": "mosaic", "colour": False,
        "name": "Parete sud"}))
    w.build_elevation(section)
    w.runner.wait()
    (entry,) = w.chunk.meta["elevations"]
    assert entry["name"] == "Parete sud" and w._raster_version == ("elevation", entry["id"])
    assert w.ortho_view.kind == "ortho"
    items = w.workspace.findItems("Prospetti", mw.Qt.MatchFlag.MatchStartsWith | mw.Qt.MatchFlag.MatchRecursive)
    assert items and items[0].childCount() == 1
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_planes_from_markers_with_their_axes_and_from_a_view():
    """Metashape's planar projection on markers: u from A towards B (0 at A), v up in the plane or
    along C -> D; the plane always faces the photos. And the plane of a view of the 3D window."""
    lean = np.radians(10)

    def wall(x, h):  # a wall leaning back 10 degrees, its foot along y = 5, facing south
        return np.array([x, 5.0 + h * np.sin(lean), 100.0 + h * np.cos(lean)])

    a, b, c = wall(2.0, 0.2), wall(6.0, 0.3), wall(4.0, 1.8)
    south = np.array([4.0, -10.0, 101.0])
    pl = E.plane_from_markers([a, b, c], a, b, toward=south)
    uvd = pl.coords(np.array([a, b, c]))
    assert np.allclose(uvd[:, 2], 0, atol=1e-9) and np.allclose(uvd[0, :2], 0, atol=1e-9)
    assert np.isclose(uvd[1, 0], np.linalg.norm(b - a)) and pl.normal @ (south - a) > 0 and pl.v_axis[2] > 0.9
    assert not E.heights_measured(pl)  # leaning: v is not the height
    # B -> A runs from right to left as seen from the south: u grows the other way, v stays up
    back = E.plane_from_markers([a, b, c], b, a, toward=south)
    assert back.coords(a[None])[0, 0] < 0 and back.v_axis[2] > 0.9 and back.normal @ (south - a) > 0
    # an upright wall with A -> B level: v is the height
    up = [np.array([x, 5.0, z]) for x, z in ((2.0, 100.2), (6.0, 100.2), (4.0, 101.8), (3.0, 100.9))]
    pl = E.plane_from_markers(up, up[0], up[1], toward=south)
    assert E.heights_measured(pl) and np.allclose(pl.coords(np.array(up))[:, 1], [p[2] for p in up])
    # a floor seen from above, the vertical axis chosen: C -> D along +y, or along -y (u flips)
    floor = [np.array(p, float) for p in ((0, 0, 50), (3, 0, 50), (0, 2, 50), (3, 2, 50.01))]
    above = np.array([1.5, 1.0, 60.0])
    pl = E.plane_from_markers(floor, floor[0], floor[1], floor[0], floor[2], toward=above)
    assert pl.u_axis[0] > 0.99 and pl.v_axis[1] > 0.99 and pl.normal[2] > 0.99
    pl = E.plane_from_markers(floor, floor[0], floor[1], floor[2], floor[0], toward=above)
    assert pl.u_axis[0] < -0.99 and pl.v_axis[1] < -0.99 and pl.normal[2] > 0.99
    with pytest.raises(ValueError):
        E.plane_from_markers([a, b, a + 2 * (b - a)], a, b, toward=south)  # on a line
    # the view: looking north at a wall, the screen up the vertical: u to the east, v the height
    pl = E.plane_from_view([4.0, 5.0, 101.0], [0, -1, 0], [0, 0, 1])
    assert np.allclose(pl.u_axis, [1, 0, 0]) and E.heights_measured(pl)
    assert np.allclose(pl.coords(np.array([[4.0, 5.0, 101.0]]))[0], [0, 101.0, 0])
    # looking down at 45 degrees from the south: no heights, the centre at (0, 0)
    pl = E.plane_from_view([4.0, 5.0, 101.0], [0, -1, 1], [0, 1, 1])
    assert not E.heights_measured(pl) and np.allclose(pl.coords(np.array([[4.0, 5.0, 101.0]]))[0], 0)
    assert pl.normal @ np.array([0, -1, 1]) > 0


def _floor_height(x, y):
    """Height above ORIGIN of the synthetic trench (testing.synthetic.make_scene) at (x, y)."""
    z = 0.08 * np.sin(x * 1.7) * np.cos(y * 2.1) + 0.04 * np.sin(x * 4.0 + 1.0)
    trench = (x > 1.0) & (x < 3.0) & (y > 0.9) & (y < 2.1)
    z = np.where(trench, z - 0.35, z)
    wall = (x > 1.7) & (x < 2.3) & (y > 1.3) & (y < 1.5)
    return np.where(wall, -0.05, z)


def test_section_across_the_trench(processed, tmp_path):
    """A section at x = 2 m across the trench, seen from the west: nothing in front of the plane, v
    is the height, and the profile follows the ground, the walls of the trench and the wall stub."""
    doc, ch = _copy(processed, tmp_path, "sezione")
    a, b = ORIGIN[:2] + [2.0, 0.4], ORIGIN[:2] + [2.0, 2.6]
    frame = E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [0.5, 1.5])
    assert frame.normal[0] < -0.99
    entry = E.build_elevation(ch, frame, resolution=0.01, depth=(-1.0, 0.5), max_angle=89.0, kind="section")
    assert entry["kind"] == "section" and entry["v_is_height"] and entry["depth"][1] == 0.0
    with rasterio.open(E.elevation_paths(ch, entry)[1]) as src:
        d = src.read(1)
    assert np.isfinite(d).mean() > 0.1 and np.nanmax(d) <= 1e-6  # nothing in front (above the ground: sky)
    lines = E.read_profile(ch, entry)
    assert lines
    from shapely.geometry import LineString

    uv = np.vstack([np.array([g.interpolate(s).coords[0] for s in np.arange(0, g.length, 0.01)])
                    for g in (LineString(line) for line in lines) if g.length > 0.01])  # a point every cm
    world = frame.point(uv[:, 0], uv[:, 1], np.zeros(len(uv)))
    y = world[:, 1] - ORIGIN[1]
    # the mesh rounds the edges of the trench and of the wall over a few centimetres (more or less from
    # one run of the Poisson reconstruction to another): the flat parts are measured away from them, and
    # per centimetre of y the profile point nearest to the true ground counts (a fold of the mesh cut by the
    # plane adds a line, it does not move the ground)
    steps = np.min(np.abs(y[:, None] - np.array([0.9, 1.3, 1.5, 2.1])[None]), axis=1)
    flat = (steps > 0.08) & (y > 0.45) & (y < 2.55)
    dev = np.abs(uv[:, 1] - (ORIGIN[2] + _floor_height(2.0, y)))
    cells = np.round(y[flat] / 0.01).astype(int)
    order = np.lexsort((dev[flat], cells))
    first = np.r_[True, np.diff(cells[order]) != 0]
    err = dev[flat][order][first]
    assert len(err) > 50 and np.median(err) < 0.01 and np.percentile(err, 90) < 0.03, \
        (len(err), np.median(err), np.percentile(err, 90))
    inside = flat & (y > 1.0) & (y < 1.25)
    assert np.median(uv[inside, 1]) < ORIGIN[2] - 0.25  # the floor of the trench, not the ground
    # the profile for CAD: in the coordinates of the elevation, v the height
    import ezdxf

    out = E.write_profile_dxf(ch, entry, tmp_path / "profilo.dxf")
    msp = ezdxf.readfile(out).modelspace()
    poly = [e for e in msp if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "PROFILO"]
    vs = np.array([q[1] for e in poly for q in e.get_points("xy")])
    assert poly and ORIGIN[2] - 0.45 < vs.min() and vs.max() < ORIGIN[2] + 0.2


def test_interface_elevations_from_markers_view_and_points(processed, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _copy(processed, tmp_path, "interfaccia")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    placed = [m for m in ch.markers if m.position is not None]
    assert len(placed) >= 3
    keys = [m.key for m in placed]
    monkeypatch.setattr(w.reference, "selected_markers", lambda: keys)
    asked = {}

    def answer(values):
        def ask(cls, parent, title, fields, intro=""):
            asked[title] = [f.name for f in fields]
            return values
        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))

    # 1. the plane of the markers (the ground plates), u from the first towards the second, a section
    answer({"source": ("markers", None), "type": "section", "axis_from": keys[0], "axis_to": keys[1],
            "vert_from": None, "vert_to": None, "front": 0.5, "back": 0.3, "resolution": 0.01,
            "blending": "mosaic", "colour": False, "name": ""})
    w.build_elevation()
    w.runner.wait()
    app.processEvents()
    (entry,) = ch.meta["elevations"]
    assert {"axis_from", "axis_to", "vert_from", "vert_to", "type"} <= set(asked["Prospetto"])
    assert entry["kind"] == "section" and entry["name"] == "Sezione sui marker"
    frame = E.frame_from_dict(entry["frame"])
    p0, p1 = (ch.to_world(m.position[None])[0] for m in placed[:2])
    assert np.isclose(frame.coords(p0[None])[0, 0], 0, atol=1e-6)
    assert w._raster_version == ("elevation", entry["id"])
    assert mw.PROFILE_LAYER in w.ortho_view.layers and "Marker" in w.ortho_view.layers
    # 2. the 3D view as it looks now, what it shows
    w.show_view(w.model_view)
    app.processEvents()
    view = w.model_view.view_frame()
    assert view is not None and np.asarray(view["corners"]).shape == (4, 3)
    answer({"type": "elevation", "extent": "view", "resolution": 0.02, "blending": "mosaic", "colour": False,
            "name": ""})
    w.model_view.elevation_requested.emit("view", view)
    w.runner.wait()
    app.processEvents()
    entry = ch.meta["elevations"][-1]
    assert entry["name"] == "Ortofoto dalla vista 3D" and "extent" in asked["Ortofoto da questa vista"]
    fr = E.frame_from_dict(entry["frame"])
    assert fr.normal @ (view["toward"] / np.linalg.norm(view["toward"])) > 0.999
    # 3. three points clicked: the first two give the horizontal axis
    pts = [m.position for m in placed[:3]]
    answer({"type": "elevation", "front": 0.3, "back": 0.3, "resolution": 0.02, "blending": "mosaic",
            "colour": False, "name": "Tre punti"})
    w.model_view.elevation_requested.emit("points", pts)
    w.runner.wait()
    app.processEvents()
    assert ch.meta["elevations"][-1]["name"] == "Tre punti" and len(ch.meta["elevations"]) == 3
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_command_line_elevation_and_section(processed, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path, "riga")
    labels = [m.label for m in ch.markers if m.position is not None][:3]
    runner = CliRunner()
    r = runner.invoke(app, ["elevation", str(doc.path), "--markers", ",".join(labels), "--resolution", "0.02", "--back", "0.3",
                            "--front", "0.3", "--name", "Sui marker"], catch_exceptions=False)
    assert r.exit_code == 0 and "Sui marker:" in r.output, r.output
    a, b = ORIGIN[:2] + [2.0, 0.4], ORIGIN[:2] + [2.0, 2.6]
    r = runner.invoke(app, ["elevation", str(doc.path), "--trace", f"{a[0]},{a[1]},{b[0]},{b[1]}", "--side",
                            f"{ORIGIN[0] + 0.5},{ORIGIN[1] + 1.5}", "--section", "--back", "1",
                            "--resolution", "0.02"], catch_exceptions=False)
    assert r.exit_code == 0 and "v = height" in r.output and "profile:" in r.output, r.output
    import pyarchinit_photogrammetry as ppg

    kinds = [e["kind"] for e in ppg.Document.open(doc.path).chunk.meta["elevations"]]
    assert kinds == ["elevation", "section"]
    r = runner.invoke(app, ["elevation", str(doc.path), "--trace", "1,2,3,4", "--markers", "a,b,c"])
    assert r.exit_code != 0
