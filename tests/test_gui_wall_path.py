"""The pane *Muro tracciato in pianta*: a wall traced on the plan, snapped onto the face of the model, its side and
heights chosen and its elevation built; the line waiting on the plan and the traces of the elevations there, sent to
QGIS one way, a double click on one showing its elevation; the refusals said in Italian; a stale elevation of a wall
rebuilt on its own grid."""
from __future__ import annotations

import os

import numpy as np
import pytest

from wall_helpers import distance_to, wall_doc

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

OFF = np.array([[0.0, -0.3], [0.3, -0.3], [0.3, 0.0]])  # the face of the L clicked 0.3 m in front of it
RES = 0.05


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def make(qapp, tmp_path, monkeypatch, wall_dataset):
    """``make(chunks=1)``: a window on the wall with its plan; ``w.said`` the warnings, ``w.drawn`` the lines handed
    by the lent drawing - each test sets ``w.next_line`` (the points the next request hands over, None: Esc)."""
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    made = []

    def build(chunks: int = 1):
        doc, chs, info = wall_doc(tmp_path / f"doc{len(made)}", wall_dataset, chunks=chunks, plan=True)
        said: list = []
        monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[3] if len(a) > 3 else "")))
        w = MainWindow(doc, autoconnect=False, check_updates=False)
        w.said, w.info, w.chs, w.requests, w.next_line = said, info, chs, [], None
        monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
        monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary)))

        def request(kind, on_finished, on_cancelled=None, hint=""):
            w.requests.append(kind)
            line, w.next_line = w.next_line, None
            if line is None:
                if on_cancelled is not None:
                    on_cancelled()
            else:
                on_finished(np.asarray(line, float))
            return True

        monkeypatch.setattr(w, "request_geometry", request)
        w.runner.show_dialog = False
        w.show()
        qapp.processEvents()
        w._show_raster("ortho")
        qapp.processEvents()
        made.append(w)
        return w

    yield build
    for w in made:
        w.runner.wait()
        w.photos.stop()
        w.setWindowModified(False)
        w.close()


def _clicked(w) -> np.ndarray:
    face = np.asarray(w.info["face"], float)
    return face + OFF


def _trace(w, line=None) -> None:
    """The line traced (default: the face clicked 0.3 m off, the last click doubled by the double click)."""
    if line is None:
        c = _clicked(w)
        line = np.vstack([c, c[-1] + [0.0003, 0.0]])
    w.next_line = line
    w.show_wall_path_pane(start=True) if w.wall_pane.clicked is None else w.wall_trace_start()
    w.runner.wait()


def _texts(w) -> list[str]:
    return [t for k, t in w.said if k == "warn"]


def test_trace_snap_side_heights_and_build(make):
    from pyarchinit_photogrammetry.gui.wall_path_actions import TRACE_PREVIEW_LAYER

    w = make()
    pane = w.wall_pane
    pane.s_resolution.setValue(RES)
    _trace(w)
    assert w.requests == ["line"]
    assert pane.state("trace") == "done" and "3 vertici" in pane.state_text("trace")
    assert pane.state("snap") == "done" and pane.r_snapped.isChecked()
    assert pane.s_v0.value() == pytest.approx(100.0, abs=0.05) and pane.s_v1.value() == pytest.approx(102.0, abs=0.05)
    assert "cm" in pane.shifts.text()  # how far the snap moved each vertex
    assert len(w.ortho_view.features(TRACE_PREVIEW_LAYER)) == 2  # clicked and snapped

    assert w.wall_build()
    w.runner.wait()
    (entry,) = w.chs[0].meta["elevations"]
    assert entry["frame"]["kind"] == "path"
    assert distance_to(w.info["face"], np.asarray(entry["frame"]["vertices"])).max() < 0.01
    assert np.allclose(entry["frame"]["clicked"], _clicked(w), atol=1e-9)  # the double click taken once
    assert w._raster_version == ("elevation", entry["id"])
    assert TRACE_PREVIEW_LAYER not in w.ortho_view.layers
    assert pane.state("build") == "done"
    assert not _texts(w)


@pytest.fixture
def recorded(monkeypatch):
    """The engine's build_elevation and snap_trace recorded (the snap done for real, the build not)."""
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import wall_trace

    rec = {"builds": [], "snaps": []}
    real_snap = wall_trace.snap_trace

    def snap(*a, **k):
        rec["snaps"].append(a)
        return real_snap(*a, **k)

    def build(chunk, frame, resolution=None, name=None, depth=None, blending="mosaic", colour_correction=None,
              **kw):
        rec["builds"].append(dict(frame=frame, resolution=resolution, name=name, depth=depth, blending=blending,
                                  colour=colour_correction, **kw))
        return {"id": "fake", "name": name}

    rec["real_build"] = E.build_elevation
    monkeypatch.setattr(wall_trace, "snap_trace", snap)
    monkeypatch.setattr(E, "build_elevation", build)
    return rec


def test_the_choices_reach_the_engine(make, recorded, monkeypatch):
    w = make()
    shown = []
    monkeypatch.setattr(w, "show_elevation", shown.append)
    pane = w.wall_pane
    pane.side.setCurrentIndex(pane.side.findData("left"))
    _trace(w)
    pane.r_clicked.setChecked(True)
    pane.s_front.setValue(0.7)
    pane.s_back.setValue(0.3)
    pane.s_before.setValue(1.0)
    pane.s_after.setValue(2.0)
    pane.s_v0.setValue(100.5)
    pane.s_v1.setValue(101.5)
    pane.s_resolution.setValue(0.02)
    pane.e_name.setText("Muro nord")
    assert w.wall_build()
    w.runner.wait()
    (b,) = recorded["builds"]
    assert np.allclose(b["frame"].vertices, _clicked(w)[::-1], atol=1e-9)  # seen from the left: the other way
    assert b["extend"] == (1.0, 2.0) and b["heights"] == (100.5, 101.5)
    assert b["depth"] == (-0.3, 0.7) and b["resolution"] == 0.02 and b["name"] == "Muro nord"
    assert shown and shown[0]["id"] == "fake"
    assert len(recorded["snaps"]) == 1

    # the snap unticked: no snap at all, the clicked line built
    w2 = make()
    w2.wall_pane.c_snap.setChecked(False)
    recorded["snaps"].clear()
    _trace(w2)
    assert recorded["snaps"] == [] and w2.wall_pane.r_clicked.isChecked()
    assert w2.wall_pane.state("snap") == "done"


def test_the_dialog_offers_the_wall_and_opens_the_pane(make, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make()
    offered = []

    def ask(parent, title, fields, intro=""):
        offered.append([c[0] for c in fields[0].choices])
        return {"source": ("path", None), "type": "elevation", "side": "auto", "front": 0.8, "back": 0.4,
                "resolution": 0.03, "blending": "disabled", "colour": False, "name": "Muro est"}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    w.build_elevation()
    assert offered == [[("path", None)]]  # nothing else here: the wall first
    pane = w.wall_pane
    dock = w.panes.dock(pane)
    g = dock.geometry()
    assert not dock.isHidden() and g.right() >= 0 and g.bottom() >= 0
    assert pane.s_front.value() == pytest.approx(0.8) and pane.s_back.value() == pytest.approx(0.4)
    assert pane.c_blending.currentData() == "disabled" and pane.e_name.text() == "Muro est"
    assert w.requests == ["line"]


def test_traces_on_the_plan_open_their_elevation_and_go_to_qgis(make):
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.gui.wall_path_actions import TRACE_PREVIEW_LAYER
    from pyarchinit_photogrammetry.io.qgis_link import classify_layer

    w = make()
    w.wall_pane.s_resolution.setValue(RES)
    _trace(w)
    assert w._plan_one_way(TRACE_PREVIEW_LAYER)
    w.wall_build()
    w.runner.wait()
    (entry,) = w.chs[0].meta["elevations"]
    w._show_raster("ortho")
    (f,) = w.ortho_view.features(E.TRACES_LAYER)
    assert f["prospetto_id"] == entry["id"] and f["prospetto"] == entry["name"]
    assert w._plan_one_way(E.TRACES_LAYER)
    layer = w._plan_layers()[E.TRACES_LAYER]
    assert classify_layer(E.TRACES_LAYER, layer["kind"]) == "linee" and layer["sent"]
    w._redraw_overlays_now(None)  # drawn again with the plan, once
    assert len(w.ortho_view.features(E.TRACES_LAYER)) == 1
    w._trace_double_clicked(E.TRACES_LAYER, f)
    assert w.drawings_frame == entry["id"]
    assert E.TRACES_LAYER not in w.ortho_view.layers  # not on the elevation
    w._redraw_overlays_now(None)
    assert E.TRACES_LAYER not in w.ortho_view.layers


def test_a_trace_the_frame_refuses_is_said_and_can_be_redrawn(make):
    from pyarchinit_photogrammetry.engine.wall_path import TraceRefused
    from pyarchinit_photogrammetry.gui.wall_path_actions import refusal_text

    w = make()
    pane = w.wall_pane
    o = np.asarray(w.info["face"], float)[0]
    crossing = o + np.array([[0.0, -0.3], [4.0, -0.3], [4.0, 1.0], [2.0, -1.0]])
    _trace(w, crossing)
    assert _texts(w) == [refusal_text(TraceRefused("crosses"))]
    assert "incrocia" in _texts(w)[0] and "crosses" not in _texts(w)[0]
    assert pane.state("trace") == "todo"
    _trace(w)
    assert pane.state("trace") == "done" and pane.state("snap") == "done" and len(_texts(w)) == 1

    w2 = make()
    for cam in w2.chs[0].cameras:
        cam.transform = None
    assert w2.wall_pane.side.currentData() == "auto"
    _trace(w2)
    assert _texts(w2) == [refusal_text(TraceRefused("side"))]
    assert w2.wall_pane.state("trace") == "todo"


def test_a_thin_margin_between_the_sides_is_asked(make, monkeypatch):
    from pyarchinit_photogrammetry.engine import wall_trace
    from pyarchinit_photogrammetry.gui import wall_path_actions as A
    from pyarchinit_photogrammetry.gui import dialogs

    w = make()
    monkeypatch.setattr(wall_trace, "side_votes", lambda ch, pts: {"left": 7.0, "right": 10.0})
    asked = []

    def choice(parent, kind, title, text, options, default=0, cancel="Annulla"):
        asked.append((text, default))
        return 0

    monkeypatch.setattr(dialogs, "choice", choice)
    _trace(w)
    assert asked and "7" in asked[0][0] and "10" in asked[0][0] and asked[0][1] == 1
    assert w.wall_pane.side.currentData() == "left"
    assert A.THIN_SIDE == 0.6


def test_an_elevation_shown_or_another_chunk_does_not_confuse_the_trace(make, recorded):
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace

    w = make(chunks=2)
    pane, ch = w.wall_pane, w.chs[0]
    # an elevation shown: the plan comes back before the line is clicked
    frame, _r = path_from_trace(ch, w.info["face"], "right", snap=False)
    entry = recorded["real_build"](ch, frame, RES)
    w.show_elevation(entry)
    assert w.drawings_frame == entry["id"]
    w.next_line = None  # Esc
    w.show_wall_path_pane(start=True)
    assert w.drawings_frame is None and w.ortho_view.kind == "ortho"
    assert pane.state("trace") == "todo" and pane.running is None
    _trace(w)
    assert pane.state("trace") == "done"
    # another chunk active with the line pending: nothing built on it
    w.doc.chunk = w.chs[1]
    assert not w.wall_build()
    assert "altro chunk" in _texts(w)[-1] and recorded["builds"] == []
    assert pane.clicked is None and pane.state("trace") == "todo"
    w.doc.chunk = ch
    _trace(w)
    assert pane.clicked is not None
    w._activate_chunk(w.chs[1])
    pane.refresh()
    assert pane.clicked is None and E.TRACES_LAYER not in w.ortho_view.layers


def test_a_stale_wall_elevation_is_rebuilt_on_its_grid(make):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine.elevation_drawings import add_units
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace

    w = make()
    ch = w.chs[0]
    frame, _r = path_from_trace(ch, w.info["face"], "right", snap=False)
    entry = E.build_elevation(ch, frame, RES)
    assert entry["scale"] == "wall"
    add_units(ch, entry["id"], [UnitPolygon(box(1.0, 100.5, 2.0, 101.0), us="9", area="1", sito="Scavo")])
    t = np.array(ch.transform, float)
    t[0, 3] += 0.05
    ch.transform = t
    assert stamps.entry_stale(ch, entry)
    new = w._rebuild_elevation(ch, entry["id"], None)
    assert new["id"] == entry["id"] and list(new["size"]) == list(entry["size"])
    assert list(new["bounds"]) == list(entry["bounds"])
    assert np.array_equal(np.asarray(new["frame"]["u_map"]), np.asarray(entry["frame"]["u_map"]))
    assert new["frame"]["vertices"] == entry["frame"]["vertices"]
    units = ch.meta["drawings"]["frames"][entry["id"]]["units"]
    assert len(units) == 1


def test_the_default_heights_are_not_cut_by_a_fine_ground(tmp_path):
    """A wall 2 m high on a ground meshed far finer than it (a vertex every 5 cm): the heights by count alone stop
    at the ground; those of the pane reach the top of the wall."""
    from types import SimpleNamespace

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine.wall_path import WallPath
    from pyarchinit_photogrammetry.gui.wall_path_actions import default_heights

    from wall_helpers import model_doc

    n = 121  # the ground: 6 x 6 m, a vertex every 5 cm
    gx, gy = np.meshgrid(np.linspace(-3.0, 3.0, n), np.linspace(-3.0, 3.0, n))
    ground = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(n * n)])
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    faces = np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])])
    k = len(ground)  # the face of the wall along y = 0, from x -2 to 2, 2 m high: two triangles
    wall = np.array([[-2.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 0.0, 2.0], [-2.0, 0.0, 2.0]])
    faces = np.vstack([faces, [[k, k + 1, k + 2], [k, k + 2, k + 3]]])
    scene = SimpleNamespace(vertices=np.vstack([ground, wall]), faces=faces, face=np.array([[-2.0, 0.0], [2.0, 0.0]]),
                            viewer="right", height=2.0, thickness=0.0, marks=[])
    _doc, ch, info = model_doc(tmp_path, scene)
    line = np.asarray(info["face"], float)
    by_count = E.height_range(ch, WallPath(line), (-0.5, 0.5))
    assert by_count[1] < 101.0  # the ground's many vertices
    v0, v1 = default_heights(ch, line, "right", 0.5, 0.5)
    assert v0 == pytest.approx(100.0, abs=0.05) and v1 == pytest.approx(102.0, abs=0.05)


def test_clicks_merged_and_a_room_closed():
    from pyarchinit_photogrammetry.gui.wall_path_actions import clean_clicks

    out = clean_clicks([[0, 0], [0.0004, 0], [5, 0], [5, 5], [0, 5], [0.02, 0.01]])
    assert len(out) == 5 and np.array_equal(out[-1], out[0]) and np.array_equal(out[1], [5, 0])
    assert len(clean_clicks([[0, 0], [1, 0], [0.01, 0.0]])) == 3  # a short line is not closed


def test_no_surface_along_the_line_is_said_with_its_rule(make, recorded, monkeypatch):
    from pyarchinit_photogrammetry.engine import elevation as E

    w = make()
    _trace(w)

    def build(*a, **k):
        raise ValueError("no surface in the depth band along the trace (a column is of the face when the model is met "
                         "at 10% of its heights at least): the length on the face needs the wall")

    monkeypatch.setattr(E, "build_elevation", build)
    w.wall_build()
    w.runner.wait()
    assert "10 %" in _texts(w)[-1] and "superficie" in _texts(w)[-1]
    assert w.wall_pane.state("build") == "todo" and not [k for k, _t in w.said if k == "error"]
