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
    from pyarchinit_photogrammetry.engine import wall_trace
    from pyarchinit_photogrammetry.gui.i18n import number

    real = wall_trace.snap_trace(w.chs[0], _clicked(w), "right")  # the report the pane shows, number by number
    assert pane.report["max_shift"] == pytest.approx(real["max_shift"], abs=1e-9)
    assert pane.report["max_shift"] == pytest.approx(np.hypot(0.3, 0.3), abs=0.01)  # the corner moved most
    assert f"al massimo di {number(100 * real['max_shift'], 1)} cm" in pane.state_text("snap")
    for i, shift in enumerate(real["shift"]):  # how far the snap moved each vertex
        assert f"{i + 1}: {number(100 * shift, 1)} cm" in pane.shifts.text()
    labels = [lab for _i, lab, _a in w.ortho_view.layers[TRACE_PREVIEW_LAYER]["features"]]
    a, b = (lab.pos() for lab in labels)  # the two names apart, not one over the other
    assert np.hypot(a.x() - b.x(), a.y() - b.y()) > 50
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
    assert not w.said  # an elevation: nothing to say

    # a section asked on the traced wall: said, the elevation made instead
    monkeypatch.setattr(mw.ParamDialog, "ask", lambda parent, title, fields, intro="": {
        "source": ("path", None), "type": "section", "side": "auto", "front": 0.5, "back": 0.5, "resolution": None,
        "blending": "mosaic", "colour": True, "name": ""})
    w.build_elevation()
    assert len(_texts(w)) == 1 and "sezione" in _texts(w)[0].lower() and w.requests == ["line", "line"]


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
        raise E.NoSurface("no surface in the depth band along the trace (a column is of the face when the model is met "
                         "at 10% of its heights at least): the length on the face needs the wall")

    monkeypatch.setattr(E, "build_elevation", build)
    w.wall_build()
    w.runner.wait()
    assert "10 %" in _texts(w)[-1] and "superficie" in _texts(w)[-1]
    assert w.wall_pane.state("build") == "todo" and not [k for k, _t in w.said if k == "error"]


def test_the_real_engine_without_surface_is_said(make):
    """Heights far over the wall: the engine finds no face along the line (its own error type), the window says why."""
    from pyarchinit_photogrammetry.engine import elevation as E

    assert issubclass(E.NoSurface, ValueError)
    w = make()
    _trace(w)
    w.wall_pane.s_resolution.setValue(RES)
    w.wall_pane.s_v0.setValue(150.0)
    w.wall_pane.s_v1.setValue(151.0)
    w.wall_build()
    w.runner.wait()
    assert "10 %" in _texts(w)[-1] and not [k for k, _t in w.said if k == "error"]
    assert not w.chs[0].meta.get("elevations")


def test_the_line_does_not_follow_into_another_project(make, recorded, wall_dataset, tmp_path):
    """Chunk keys start at 0 in every project: the line of project A is not the line of chunk 0 of project B."""
    from pyarchinit_photogrammetry.gui.wall_path_actions import TRACE_PREVIEW_LAYER

    w = make()
    _trace(w)
    assert w.wall_pane.clicked is not None
    doc_b, (ch_b,), _info = wall_doc(tmp_path / "b", wall_dataset, plan=True)
    assert ch_b.key == w.chs[0].key
    w.set_document(doc_b)
    w.runner.wait()
    pane = w.wall_pane
    assert pane.clicked is None and pane.state("trace") == "todo" and pane.state_text("trace") == ""
    w._show_raster("ortho")
    assert TRACE_PREVIEW_LAYER not in w.ortho_view.layers
    assert not w.wall_build() and recorded["builds"] == []
    # the line kept on the chunk object itself: the same key in a new document is not enough
    _trace(w)
    assert pane.state("trace") == "done"


def test_the_traces_are_drawn_once_per_raster_load(make, monkeypatch, qapp, wall_dataset, tmp_path):
    from pyarchinit_photogrammetry.engine import elevation as E

    w = make()
    calls = []
    real = E.trace_layer
    monkeypatch.setattr(E, "trace_layer", lambda ch: calls.append(1) or real(ch))
    w._show_raster("ortho")
    qapp.processEvents()
    assert len(calls) == 1
    calls.clear()
    w.refresh_all()  # the window shown again: the plan loaded once, its traces drawn once
    qapp.processEvents()
    assert len(calls) == 1
    calls.clear()
    doc_b, _chs, _info = wall_doc(tmp_path / "b", wall_dataset, chunks=2, plan=True)
    w.set_document(doc_b)  # another project
    qapp.processEvents()
    assert len(calls) == 1
    calls.clear()
    w._activate_chunk(doc_b.chunks[1])  # another chunk
    qapp.processEvents()
    assert len(calls) == 1


def test_the_pane_fits_the_right_dock(make, qapp):
    w = make()
    _trace(w)
    pane = w.wall_pane
    w.show_wall_path_pane()
    qapp.processEvents()
    assert pane.minimumSizeHint().width() <= 260


def _menu_entry(w, monkeypatch, open_menu, text):
    """Trigger the entry ``text`` of the context menu ``open_menu()`` opens (the window's QMenu stood in by one whose
    exec triggers that entry)."""
    from PySide6.QtWidgets import QMenu

    from pyarchinit_photogrammetry.gui import main_window as mw

    found = []

    class Menu(QMenu):
        def exec(self, *_a, **_k):
            for a in self.actions():
                if a.text() == text:
                    found.append(a)
                    a.trigger()
                    return a
            return None

    monkeypatch.setattr(mw, "QMenu", Menu)
    open_menu()
    assert found, f"no menu entry {text!r}"


def test_campaigns_from_the_workspace_menu(make, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_groups as G
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace
    from pyarchinit_photogrammetry.gui import dialogs
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make(chunks=3)
    a, b, c = w.chs
    c.crs = "EPSG:32634"                                                    # another system: offered, not ticked
    frame, _r = path_from_trace(a, w.info["face"], "right", snap=False)
    entry = E.build_elevation(a, frame, RES, "Muro", (-0.5, 0.5), max_angle=89.0, heights=(100.0, 102.0))
    asked = []
    answers = [{"chunks": [b.key, c.key], f"date_{a.key}": "2017-11-05", f"date_{b.key}": "2022-11-03",
                f"date_{c.key}": ""}]

    def ask(parent, title, fields, intro=""):
        asked.append({f.name: f for f in fields})
        return answers.pop(0)

    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(ask))
    monkeypatch.setattr(dialogs, "choice", lambda *a, **k: pytest.fail("no question for chunks in a system"))
    _menu_entry(w, monkeypatch, lambda: w._workspace_menu("elevation", a, entry, QPoint(0, 0)),
                mw.tr("Stesso prospetto su altri chunk..."))
    w.runner.wait()
    (fields,) = asked
    assert [v for v, _lab in fields["chunks"].choices] == [b.key, c.key] and fields["chunks"].default == [b.key]
    assert fields["chunks"].kind == "checklist" and fields[f"date_{b.key}"].kind == "text"
    assert fields[f"date_{a.key}"].kind == "text"                          # the reference's date too
    (group,) = G.groups(a)
    assert [(m["chunk"], m["date"]) for m in group["members"]] == [(a.key, "2017-11-05"), (b.key, "2022-11-03")]
    (new,) = E.elevations(b)
    assert new["grid_of"] == entry["id"]
    infos = [t for k, t in w.said if k == "message"]
    assert len(infos) == 1
    assert mw.tr("Costruito su {0} chunk: {1}.", 1, b.label) in infos[0]
    assert mw.tr("«{0}» lasciato fuori: {1}.", c.label, mw.tr(
        "è in {0}, il prospetto in {1}: la stessa griglia vuole lo stesso sistema di riferimento",
        "EPSG:32634", "EPSG:32633")) in infos[0]
    w.workspace.populate(w.doc)                                             # the tooltip of a member
    tips = []
    it = QTreeWidgetItemIterator(w.workspace)
    while it.value():
        data = it.value().data(0, 0x0100)
        if isinstance(data, tuple) and data and data[0] == "elevation" and data[2]["id"] == new["id"]:
            tips.append(it.value().toolTip(0))
        it += 1
    assert tips and "Muro" in tips[0] and "2022-11-03" in tips[0]

    # again: the members' dates offered as they are, a member not ticked; dates edited, nothing built
    asked.clear()
    answers.append({"chunks": [], f"date_{a.key}": "2017-11-06", f"date_{b.key}": "2022-11-03",
                    f"date_{c.key}": ""})
    w.wall_campaigns(entry, a)
    w.runner.wait()
    (fields,) = asked
    assert fields["chunks"].default == [] and fields[f"date_{b.key}"].default == "2022-11-03"
    assert [m["date"] for m in group["members"]] == ["2017-11-06", "2022-11-03"]
    assert len(E.elevations(b)) == 1

    # two local chunks: the question asked; declined: nothing built; accepted: built
    for ch in w.chs:
        ch.crs = "LOCAL"
    entry["crs"] = "LOCAL"                                                  # a project in a local frame
    questions = []
    replies = [None, 0]
    monkeypatch.setattr(dialogs, "choice", lambda *a, **k: questions.append(a) or replies.pop(0))
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"chunks": [c.key], f"date_{a.key}": "", f"date_{b.key}": "",
                                                 f"date_{c.key}": ""}))
    assert not w.wall_campaigns(entry, a)
    w.runner.wait()
    assert len(questions) == 1 and E.elevations(c) == [] and len(G.groups(a)[0]["members"]) == 2
    assert w.wall_campaigns(entry, a)
    w.runner.wait()
    assert len(questions) == 2 and len(E.elevations(c)) == 1 and len(G.groups(a)[0]["members"]) == 3


def test_reason_text_by_code():
    from pyarchinit_photogrammetry.gui.i18n import tr
    from pyarchinit_photogrammetry.gui.wall_path_actions import reason_text

    assert reason_text({"code": "crs", "reason": "the elevation is in EPSG:32633, the chunk in EPSG:32634: x",
                        "theirs": "EPSG:32633", "mine": "EPSG:32634"}) == tr(
        "è in {0}, il prospetto in {1}: la stessa griglia vuole lo stesso sistema di riferimento",
        "EPSG:32634", "EPSG:32633")
    assert reason_text({"code": "already", "reason": "x"}) == tr("ha già un prospetto del gruppo")
    assert reason_text({"code": "no_model", "reason": "x"}) == tr("non ha un modello")
    assert reason_text({"code": "local", "reason": "x"}) == tr(
        "è in un sistema locale: non è stato detto che è nello stesso sistema")


def test_campaigns_step_of_the_pane(make, monkeypatch):
    from pyarchinit_photogrammetry.engine import elevation_groups as G
    from pyarchinit_photogrammetry.gui import dialogs
    from pyarchinit_photogrammetry.gui.wall_path_pane import STEPS

    assert STEPS[-1] == "campaigns"
    w = make(chunks=2)
    pane = w.wall_pane
    pane.s_resolution.setValue(RES)
    pane.c_snap.setChecked(False)
    _trace(w)
    assert pane.state("campaigns") == "todo" and not pane.b_campaigns.isEnabled()
    assert w.wall_build()
    w.runner.wait()
    assert pane.b_campaigns.isEnabled()
    a, b = w.chs
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"chunks": [b.key], f"date_{b.key}": "2022-11-03"}))
    pane.b_campaigns.click()
    w.runner.wait()
    assert len(G.groups(a)[0]["members"]) == 2
    assert pane.state("campaigns") == "done" and "2" in pane.state_text("campaigns")


def test_the_sheet_from_the_menu(make, monkeypatch, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QFileDialog

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_groups as G
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make(chunks=2)
    a, b = w.chs
    frame, _r = path_from_trace(a, w.info["face"], "right", snap=False)
    entry = E.build_elevation(a, frame, RES, "Muro", (-0.5, 0.5), max_angle=89.0, heights=(100.0, 102.0))
    entries = []

    def entries_of_menu():
        class Spy(mw.QMenu):
            def exec(self, *_a, **_k):
                entries.extend(x.text() for x in self.actions())

        monkeypatch.setattr(mw, "QMenu", Spy)
        w._workspace_menu("elevation", a, entry, QPoint(0, 0))
        return entries

    original = mw.QMenu
    assert mw.tr("Scheda di confronto...") not in entries_of_menu()     # alone: no sheet
    monkeypatch.setattr(mw, "QMenu", original)
    (_ch, new), = G.build_on_chunks(w.doc, a, entry, [b])["built"]
    out = tmp_path / "scheda.png"
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: asked.append(a) or (str(out), ""))
    _menu_entry(w, monkeypatch, lambda: w._workspace_menu("elevation", b, new, QPoint(0, 0)),
                mw.tr("Scheda di confronto..."))
    w.runner.wait()
    assert len(asked) == 1 and out.exists() and (tmp_path / "scheda.pdf").exists()
    infos = [t for k, t in w.said if k == "message"]
    assert len(infos) == 1 and str(out) in infos[0] and str(tmp_path / "scheda.pdf") in infos[0]

    # from the pane: its button waits for a group of two
    pane = w.wall_pane
    assert not pane.b_sheet.isEnabled()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))       # cancelled: nothing
    assert not w.wall_sheet(entry, a)
    out.unlink()                                                        # the PDF beside it is still there
    pdf = tmp_path / "scheda.pdf"
    pdf.write_bytes(b"mine")
    from pyarchinit_photogrammetry.gui import dialogs

    questions, replies = [], [None, 0]
    monkeypatch.setattr(dialogs, "choice", lambda *a, **k: questions.append(a) or replies.pop(0))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    assert not w.wall_sheet(entry, a)                                   # declined: nothing written
    w.runner.wait()
    assert len(questions) == 1 and "scheda.pdf" in questions[0][3]
    assert not out.exists() and pdf.read_bytes() == b"mine"
    assert w.wall_sheet(entry, a)                                       # accepted: both replaced
    w.runner.wait()
    assert out.exists() and pdf.read_bytes()[:4] == b"%PDF"


def test_the_sheet_step_of_the_pane(make, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    from pyarchinit_photogrammetry.gui import dialogs

    w = make(chunks=2)
    pane = w.wall_pane
    pane.s_resolution.setValue(RES)
    pane.c_snap.setChecked(False)
    _trace(w)
    assert w.wall_build()
    w.runner.wait()
    assert not pane.b_sheet.isEnabled()
    a, b = w.chs
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"chunks": [b.key], f"date_{b.key}": "2022-11-03"}))
    pane.b_campaigns.click()
    w.runner.wait()
    assert pane.b_sheet.isEnabled()
    out = tmp_path / "pane.jpg"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    pane.b_sheet.click()
    w.runner.wait()
    assert open(out, "rb").read(3) == b"\xff\xd8\xff" and (tmp_path / "pane.pdf").exists()


def _tip_of(w, eid) -> str:
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    w.workspace.populate(w.doc)
    it = QTreeWidgetItemIterator(w.workspace)
    while it.value():
        data = it.value().data(0, 0x0100)
        if isinstance(data, tuple) and data and data[0] == "elevation" and data[2]["id"] == eid:
            return it.value().toolTip(0)
        it += 1
    raise AssertionError(f"no elevation {eid} in the workspace")


def test_composite_from_the_menu_stale_said_and_left_by_hand(make, monkeypatch):
    from PySide6.QtCore import QPoint

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_composite as C
    from pyarchinit_photogrammetry.engine import elevation_groups as G
    from pyarchinit_photogrammetry.engine import patches as P
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace
    from pyarchinit_photogrammetry.gui import dialogs
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make(chunks=2)
    a, b = w.chs
    frame, _r = path_from_trace(a, w.info["face"], "right", snap=False)
    entry = E.build_elevation(a, frame, RES, "Muro", (-0.5, 0.5), max_angle=89.0, heights=(100.0, 102.0))
    (_ch, new), = G.build_on_chunks(w.doc, a, entry, [b], dates={a.key: "2017-11-05", b.key: "2022-11-03"})["built"]
    asked = []

    def ask(parent, title, fields, intro=""):
        asked.append({f.name: f for f in fields})
        return {"threshold": 0.2, "name": "Composito del muro", "match": True}

    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(ask))
    _menu_entry(w, monkeypatch, lambda: w._workspace_menu("elevation", b, new, QPoint(0, 0)),
                mw.tr("Composito delle campagne..."))
    w.runner.wait()
    (fields,) = asked
    assert fields["threshold"].default == C.DEEPER and fields["match"].default is True
    (comp,) = [e for e in E.elevations(a) if e["kind"] == "composite"]
    assert comp["name"] == "Composito del muro" and comp["threshold"] == 0.2
    assert w.chunk is a and w.drawings_frame == comp["id"]                 # shown
    assert not C.is_stale(w.doc, a, comp)
    stale_text = mw.tr("Composito da rifare: una campagna è cambiata dopo che è stato fatto")
    assert stale_text not in _tip_of(w, comp["id"])

    new["build"] = "0" * 12                                                # a campaign built again
    assert stale_text in _tip_of(w, comp["id"])
    w.show_elevation(comp)
    assert stale_text in w.statusBar().currentMessage()

    # made again from the menu of the composite: the same entry, current
    _menu_entry(w, monkeypatch, lambda: w._workspace_menu("elevation", a, comp, QPoint(0, 0)),
                mw.tr("Rifai il composito"))
    w.runner.wait()
    (again,) = [e for e in E.elevations(a) if e["kind"] == "composite"]
    assert again["id"] == comp["id"] and again["threshold"] == 0.2 and len(asked) == 1   # no dialog
    assert not C.is_stale(w.doc, a, again) and stale_text not in _tip_of(w, comp["id"])

    # the patch tool on it warns
    w.show_elevation(again)
    w.said.clear()
    w.start_patch()
    assert any(mw.tr(P.COMPOSITE_IT) in t for t in _texts(w))
    assert not w.draw_actions["patch"].isChecked() if "patch" in w.draw_actions else True

    # the chunk moved: the composite is listed to be made by hand, with no step for it
    t = np.array(a.transform, float)
    t[0, 3] += 0.05
    a.transform = t
    steps, by_hand, _versions = w._rebuild_plan(a)
    assert again["name"] in by_hand and again["name"] not in [name for name, _fn in steps]
    assert entry["name"] in [name for name, _fn in steps]


def test_composite_step_of_the_pane(make, monkeypatch):
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.gui import dialogs

    w = make(chunks=2)
    pane = w.wall_pane
    pane.s_resolution.setValue(RES)
    pane.c_snap.setChecked(False)
    _trace(w)
    assert w.wall_build()
    w.runner.wait()
    assert not pane.b_composite.isEnabled() and not pane.b_recomposite.isEnabled()
    a, b = w.chs
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"chunks": [b.key], f"date_{a.key}": "2017-11-05",
                                                 f"date_{b.key}": "2022-11-03"}))   # a composite needs every date
    pane.b_campaigns.click()
    w.runner.wait()
    assert pane.b_composite.isEnabled() and not pane.b_recomposite.isEnabled()
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"threshold": 0.15, "name": "", "match": False}))
    pane.b_composite.click()
    w.runner.wait()
    (comp,) = [e for e in E.elevations(a) if e["kind"] == "composite"]
    assert comp["colours_matched"] is False and pane.b_recomposite.isEnabled()
    assert "composito" in pane.state_text("campaigns").lower()
    pane.b_recomposite.click()
    w.runner.wait()
    assert [e["id"] for e in E.elevations(a) if e["kind"] == "composite"] == [comp["id"]]


def test_composite_refused_while_a_campaign_has_no_date(make, monkeypatch):
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_composite as C
    from pyarchinit_photogrammetry.engine import elevation_groups as G
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace
    from pyarchinit_photogrammetry.gui import dialogs
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make(chunks=2)
    a, b = w.chs
    frame, _r = path_from_trace(a, w.info["face"], "right", snap=False)
    entry = E.build_elevation(a, frame, RES, "Muro", (-0.5, 0.5), max_angle=89.0, heights=(100.0, 102.0))
    r = G.build_on_chunks(w.doc, a, entry, [b], dates={a.key: "2017-11-05", b.key: "2022-11-03"})
    (_ch, new), = r["built"]
    G.set_date(r["group"], b.key, new["id"], None)
    asked = []
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(lambda *a, **k: asked.append(1)))
    w.said.clear()
    assert not w.wall_composite(new, b)
    assert asked == [] and not C.composites(a)
    assert mw.tr(C.UNDATED_IT, b.label) in _texts(w)
    assert mw.tr(C.UNDATED_IT, b.label).startswith(f"Dai una data a «{b.label}»")


def test_finish_from_the_menu_and_the_backlit_field(make, recorded, monkeypatch, tmp_path):
    """*Esporta per la pubblicazione...* in the menu of an elevation and in the pane: the dialog's choices and the path
    reach the finishing; the field of the backlit photos in every elevation dialog and in the pane reaches the build,
    and the rebuild of an elevation passes its own setting on."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QFileDialog

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_finish as F
    from pyarchinit_photogrammetry.engine.wall_trace import path_from_trace
    from pyarchinit_photogrammetry.gui import dialogs
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = make()
    a = w.chs[0]
    frame, _r = path_from_trace(a, w.info["face"], "right", snap=False)
    entry = recorded["real_build"](a, frame, RES, "Muro", (-0.5, 0.5), max_angle=89.0, heights=(100.0, 102.0))
    calls = []

    def finish(chunk, entry_, out_path, **kw):
        calls.append(dict(chunk=chunk, entry=entry_, out=out_path, **kw))
        return {"path": str(out_path), "size": [10, 5], "filled": 3, "masked": 4, "targets": 1, "openings": 7,
                "levelled": False}

    monkeypatch.setattr(F, "finish", finish)
    answers = {"fill": True, "mask": False, "level": False, "targets": True, "reduce": 2, "max_hole": 0.3}
    asked = []
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": asked.append([f.name for f in fields]) or dict(answers)))
    out = tmp_path / "muro.jpg"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a_, **k: (str(out), ""))
    _menu_entry(w, monkeypatch, lambda: w._workspace_menu("elevation", a, entry, QPoint(0, 0)),
                mw.tr("Esporta per la pubblicazione..."))
    w.runner.wait()
    (c,) = calls
    assert c["chunk"] is a and c["entry"]["id"] == entry["id"] and str(c["out"]) == str(out)
    assert (c["fill"], c["mask"], c["level"], c["targets"], c["reduce"], c["max_hole"]) == \
        (True, False, False, True, 2, 0.3)
    assert set(asked[0]) >= {"fill", "mask", "level", "targets", "reduce", "max_hole"}
    said = [t for k, t in w.said if k == "message" and str(out) in t]
    assert said and mw.tr("{0} aperture lasciate aperte", 7) in said[0]
    # inside the project's data: refused before the work starts
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a_, **k: (str(a.data_dir / "x.jpg"), ""))
    assert not w.wall_finish(entry, a)
    assert len(calls) == 1 and w.said[-1][0] == "warn"
    # from the pane: waits for an elevation built
    pane = w.wall_pane
    assert not pane.b_finish.isEnabled()
    assert not w.wall_finish()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a_, **k: (str(tmp_path / "pane.tif"), ""))
    pane.chunk, pane.built = a, entry["id"]
    pane.update_states()
    assert pane.b_finish.isEnabled()
    pane.b_finish.click()
    w.runner.wait()
    assert len(calls) == 2 and str(calls[1]["out"]) == str(tmp_path / "pane.tif")

    # the field of the backlit photos: in every elevation dialog, to the build
    label = mw.tr("Lascia fuori le foto in controluce (oltre il 2 % di pixel bruciati)")
    for kind in ("plane", "cylinder", "view", "points"):
        field = next(f for f in w._elevation_fields(kind) if f.name == "skip_backlit")
        assert field.label == label and field.default is False
    monkeypatch.setattr(dialogs.ParamDialog, "ask", staticmethod(
        lambda parent, title, fields, intro="": {"extent": "all", "skip_backlit": True, "name": "",
                                                 "resolution": None, "blending": "mosaic", "colour": True}))
    w.build_elevation(view={"centre": [500003.0, 4400000.0, 101.0], "toward": [0.0, -1.0, 0.0],
                            "up": [0.0, 0.0, 1.0], "corners": []})
    w.runner.wait()
    assert recorded["builds"][-1]["skip_backlit"] is True
    pane.set_values({"skip_backlit": True})
    assert pane.c_backlit.isChecked() and pane.l_backlit.text() == label
    pane.c_snap.setChecked(False)
    _trace(w)
    assert w.wall_build()
    w.runner.wait()
    assert recorded["builds"][-1]["skip_backlit"] is True
    pane.c_backlit.setChecked(False)
    assert w.wall_build()
    w.runner.wait()
    assert recorded["builds"][-1]["skip_backlit"] is False

    # the rebuild passes the elevation's own setting on
    got = []

    class Stop(Exception):
        pass

    def capture(*a_, **k):
        got.append(k)
        raise Stop

    monkeypatch.setattr(E, "build_elevation", capture)
    entry["skip_backlit"] = True
    with pytest.raises(Stop):
        w._rebuild_elevation(a, entry["id"], None)
    assert got[0]["skip_backlit"] is True


CREST = np.array([[0.0, 0.2], [-0.2, 0.2], [-0.2, 0.0]])  # the face of the L moved half its thickness into the wall


def test_follow_and_proposals_in_the_pane(make, monkeypatch):
    from pyarchinit_photogrammetry.ai import sam_env
    from pyarchinit_photogrammetry.engine import wall_auto
    from pyarchinit_photogrammetry.gui.wall_path_actions import PROPOSALS_LAYER, TRACE_PREVIEW_LAYER
    from wall_helpers import share_within

    monkeypatch.setattr(sam_env, "available", lambda: None)
    w = make()
    pane = w.wall_pane
    assert pane.b_follow.text() == "Segui il muro fra due clic"
    assert pane.b_propose.text() == "Proponi i muri"
    assert pane.c_sam.text() == "Usa SAM dove il muro è basso"
    assert not pane.c_sam.isEnabled()  # no SAM: the tick is off
    assert not pane.b_keep.isEnabled() and not pane.b_via.isEnabled()  # nothing proposed yet
    calls = []
    real = wall_auto.follow

    def follow(chunk, points, mask=None, **kw):
        calls.append((np.asarray(points, float).copy(), mask))
        return real(chunk, points, mask=mask, **kw)
    monkeypatch.setattr(wall_auto, "follow", follow)
    crest = np.asarray(w.info["face"], float) + CREST
    clicks = np.array([crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])
    w.next_line = clicks
    assert w.wall_follow()
    w.runner.wait()
    assert w.requests[-1] == "line"
    assert np.allclose(calls[-1][0], clicks) and calls[-1][1] is None  # the two clicks reach the engine
    assert pane.proposal is not None and pane.state("trace") == "todo"  # proposed, not yet kept
    assert share_within(pane.proposal["points"], crest, 0.2) >= 0.9
    assert [f["linea"] for f in w.ortho_view.features(TRACE_PREVIEW_LAYER)] == ["proposta", "agganciata"]
    assert pane.b_keep.isEnabled() and pane.b_via.isEnabled()
    assert not _texts(w)

    via = crest[1] + [-1.0, 0.0]  # a via point: the clicks again with it in between
    w.next_line = np.array([via])
    assert w.wall_follow_via()
    w.runner.wait()
    assert w.requests[-1] == "point"
    assert np.allclose(calls[-1][0], [clicks[0], via, clicks[1]])

    proposed = np.asarray(pane.proposal["points"], float).copy()
    assert distance_to(proposed, via[None])[0] < 0.1
    assert w.wall_follow_keep()
    w.runner.wait()
    assert pane.state("trace") == "done" and pane.proposal is None
    assert np.array_equal(pane.clicked, proposed)  # the proposal's vertices are the line of the wall
    assert [f["linea"] for f in w.ortho_view.features(TRACE_PREVIEW_LAYER)] == ["cliccata", "agganciata"]
    assert pane.state("snap") == "done" and pane.r_snapped.isChecked()
    assert distance_to(w.info["face"], pane.snapped).max() < 0.05
    assert pane.s_v1.value() > pane.s_v0.value()
    assert not _texts(w)

    far = np.array([[0.0, -5.0], [4.0, -5.0]]) + np.asarray(w.info["face"][0])  # open ground: a weak line, said
    w.next_line = far
    assert w.wall_follow()
    w.runner.wait()
    assert pane.proposal is not None and pane.proposal["on_ridge"] < 0.5
    assert any("debole" in t for t in _texts(w))
    w.said.clear()

    assert w.wall_proposals()
    w.runner.wait()
    feats = w.ortho_view.features(PROPOSALS_LAYER)
    assert len(feats) == 1 and share_within(feats[0]["_geom"][1], crest, 0.2) >= 0.9
    w.ortho_view.feature_clicked.emit(PROPOSALS_LAYER, dict(feats[0]))  # a click on the candidate: the trace
    w.runner.wait()
    assert pane.proposal is None and pane.state("trace") == "done"
    assert np.allclose(pane.clicked, feats[0]["_geom"][1])
    assert pane.state("snap") == "done"
    assert not _texts(w)


def test_follow_with_sam_makes_the_mask_in_the_task(make, monkeypatch):
    from pyarchinit_photogrammetry.ai import sam_env
    from pyarchinit_photogrammetry.engine import wall_auto

    monkeypatch.setattr(sam_env, "available", lambda: "external")
    w = make()
    pane = w.wall_pane
    pane.refresh()
    assert pane.c_sam.isEnabled()
    pane.c_sam.setChecked(True)
    made = []

    class Fake:
        def segment_points(self, image, points, labels):
            made.append(np.asarray(points).shape)
            return np.zeros(np.asarray(image).shape[:2], bool)
    monkeypatch.setattr(w, "_segmenter", lambda: Fake())
    masks = []
    real = wall_auto.follow
    monkeypatch.setattr(wall_auto, "follow", lambda ch, pts, mask=None, **kw: (masks.append(mask),
                                                                              real(ch, pts, mask=mask, **kw))[1])
    crest = np.asarray(w.info["face"], float) + CREST
    w.next_line = np.array([crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])
    assert w.wall_follow()
    w.runner.wait()
    assert made == [(2, 2)] and masks[-1] is not None and masks[-1][0].dtype == bool


def test_a_followed_line_kept_asks_the_side_on_a_thin_margin(make, recorded, monkeypatch):
    from pyarchinit_photogrammetry.engine import wall_trace
    from pyarchinit_photogrammetry.gui import dialogs

    w = make()
    pane = w.wall_pane
    crest = np.asarray(w.info["face"], float) + CREST
    w.next_line = np.array([crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])
    assert w.wall_follow()
    w.runner.wait()
    assert pane.proposal["which"] == "right" and recorded["snaps"][-1][2] == "right"
    monkeypatch.setattr(wall_trace, "side_votes", lambda ch, pts: {"left": 7.0, "right": 10.0})
    asked = []

    def choice(parent, kind, title, text, options, default=0, cancel="Annulla"):
        asked.append((text, default))
        return 0  # the owner says: from the left
    monkeypatch.setattr(dialogs, "choice", choice)
    assert w.wall_follow_keep()
    w.runner.wait()
    assert asked and "7" in asked[0][0] and "10" in asked[0][0] and asked[0][1] == 1
    assert pane.side.currentData() == "left"
    assert recorded["snaps"][-1][2] == "left"  # snapped again on the side chosen
    assert pane.state("trace") == "done" and pane.state("snap") == "done"

    pane.side.setCurrentIndex(pane.side.findData("auto"))  # the kept line snapped again from the photos' side
    w.runner.wait()
    w.next_line = np.array([crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])
    assert w.wall_follow()
    w.runner.wait()
    monkeypatch.setattr(dialogs, "choice", lambda *a, **k: None)  # the owner gives up: nothing kept
    assert not w.wall_follow_keep()
    assert pane.proposal is not None


def test_what_cannot_be_followed_is_said_by_its_kind():
    from pyarchinit_photogrammetry.gui.wall_path_actions import follow_error_text

    assert "punto 2 è fuori dal DEM" in follow_error_text({"error": "outside", "point": 2})
    assert "due clic" in follow_error_text({"error": "points"})
    assert "Nessun percorso" in follow_error_text({"error": "no_path"})
    assert "costruiscila prima" in follow_error_text({"error": "no_ortho"})
    assert "fuori dall'ortofoto" in follow_error_text({"error": "sam_outside"})
    assert "SAM 2.1 non ha potuto" in follow_error_text({"error": "sam", "detail": "x"})
