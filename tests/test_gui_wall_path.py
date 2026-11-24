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
