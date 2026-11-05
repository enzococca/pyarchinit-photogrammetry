"""Flusso di lavoro > Orienta il sistema locale: the dialog filled by picks in the 3D view, by the markers and by a
click on the orthophoto, the new axes previewed, the orientation applied in the background and taken back and done
again as one step of Modifica > Annulla; the refusals said in the window's words; the fitted plane from a lasso."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, processed, tmp_path, monkeypatch):
    """The window on a LOCAL, tilted copy of the trench with one drawing of each store (``orient_helpers.drawn``);
    the modal boxes recorded in ``w.said`` instead of shown."""
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from orient_helpers import drawn, local_copy

    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc, ch, t_bad = local_copy(processed, tmp_path)
    drawn(ch)
    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")))
    w = MainWindow(doc, autoconnect=False)
    w.said = said
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_info", lambda title, text: said.append(("info", text)))
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    yield w
    dlg = getattr(w, "_orient_dialog", None)
    if dlg is not None:
        try:
            dlg.close()
        except RuntimeError:  # already deleted
            pass
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _surface_points(ch, n=3):
    """Three points of the dense cloud (internal frame) spread over the trench, as ``test_orient`` picks them."""
    xyz = np.asarray(ch.point_cloud.xyz, float)
    c = np.median(xyz, axis=0)
    _, _, vt = np.linalg.svd(xyz[::50] - c, full_matrices=False)
    span = np.ptp((xyz - c) @ vt[:2].T, axis=0)
    targets = [c - 0.3 * span[0] * vt[0], c + 0.3 * span[0] * vt[0], c + 0.3 * span[1] * vt[1]]
    return np.array([xyz[np.argmin(np.linalg.norm(xyz - t, axis=1))] for t in targets[:n]])


def _open(win, qapp):
    win.orient_local()
    qapp.processEvents()
    dlg = win._orient_dialog
    assert dlg is not None and dlg.isVisible()
    return dlg


def _pick_three(win, qapp, dlg):
    ch = win.chunk
    internal = _surface_points(ch)
    for label, p in zip("ABC", internal):
        win.model_view.orient_requested.emit("point", (label, p))
    qapp.processEvents()
    return ch.to_world(internal)


def test_three_picks_fill_the_dialog_and_preview(win, qapp):
    dlg = _open(win, qapp)
    assert not dlg.b_apply.isEnabled()  # nothing picked yet
    points = _pick_three(win, qapp, dlg)
    for label, p in zip("ABC", points):
        assert np.allclose(dlg.point(label), p, atol=1e-9)
        assert dlg.rows[label]["coords"].text() != "-"
    axes = win.model_view.sd.axes
    assert axes is not None
    from pyarchinit_photogrammetry.engine import orient

    frame = orient.frame_from_points(*points)
    assert np.allclose(axes[1], frame.axes, atol=1e-9)
    assert axes[2] >= 0.05
    win.show_view(win.model_view)
    qapp.processEvents()  # the view laid out and sized before it is painted (it was not, once, under load)
    shown = win.model_view.canvas.grab().toImage()  # the axes painted over the view
    from PySide6.QtGui import QColor

    from pyarchinit_photogrammetry.gui.model_view import AXES_COLOURS

    # the x axis on the screen: its projected ends in the view, and a near-red pixel along it (the line is
    # antialiased and, for some builds of the fixture's model, short: an exact colour is not always there)
    canvas = win.model_view.canvas
    origin, rows, size = canvas.sd.axes
    ends = np.vstack([origin, np.asarray(origin) + size * np.asarray(rows)[0]])
    w, h = canvas.width(), canvas.height()
    px, py, _depth, ok = canvas.cam.project(ends, w, h)
    assert ok.all(), "the x axis is not in the view"
    length = float(np.hypot(px[1] - px[0], py[1] - py[0]))
    assert length >= 3, length
    # y and z are painted after x: where one of them lies on the screen over x (the same direction: one pointing the
    # other way does not cover it) the red is covered, and the projected-ends checks above are all that can be seen
    tips = np.asarray(origin) + size * np.asarray(rows)
    tx, ty, _td, _tok = canvas.cam.project(np.vstack([origin, tips]), w, h)
    along = np.array([tx[1] - tx[0], ty[1] - ty[0]], float)
    covered = False
    for k in (2, 3):
        d = np.array([tx[k] - tx[0], ty[k] - ty[0]], float)
        if np.hypot(*d) > 1e-9 and float(along @ d) / (np.hypot(*along) * np.hypot(*d)) > np.cos(np.radians(5)):
            covered = True
    red = QColor(AXES_COLOURS[0])
    ratio = shown.width() / w
    found = covered
    for t in np.linspace(0.15, 0.85, 30):  # along the axis, away from the origin where y and z start too
        cx, cy = px[0] + t * (px[1] - px[0]), py[0] + t * (py[1] - py[0])
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                ix, iy = int(round((cx + dx) * ratio)), int(round((cy + dy) * ratio))
                if 0 <= ix < shown.width() and 0 <= iy < shown.height():
                    c = shown.pixelColor(ix, iy)
                    if abs(c.red() - red.red()) <= 40 and abs(c.green() - red.green()) <= 40 \
                            and abs(c.blue() - red.blue()) <= 40:
                        found = True
    assert found, "no pixel of the x axis"
    assert "°" in dlg.summary.text()
    assert dlg.b_apply.isEnabled()
    dlg.reject()
    qapp.processEvents()
    assert win.model_view.sd.axes is None  # closed: the preview goes


def test_markers_combo_and_raster_pick(win, qapp):
    from orient_helpers import centre

    from pyarchinit_photogrammetry.archaeo.measures import ground_dem_at

    dlg = _open(win, qapp)
    ch = win.chunk
    placed = [m for m in ch.markers if m.position is not None]
    combo = dlg.rows["A"]["combo"]
    i = combo.findData(placed[0].key)
    assert i > 0 and combo.itemText(0) == "-"
    combo.setCurrentIndex(i)
    assert np.allclose(dlg.point("A"), ch.to_world(np.asarray(placed[0].position, float)[None])[0], atol=1e-9)
    # B: a click on the orthophoto, z from the DEM there
    assert dlg.rows["B"]["b2d"].isEnabled()
    dlg.rows["B"]["b2d"].click()
    assert win.ortho_view.mode == "orient_pick"
    x, y, _z = centre(ch)
    win.ortho_view.orient_clicked.emit(float(x), float(y))
    qapp.processEvents()
    assert win.ortho_view.mode == "pan"
    want = ground_dem_at(ch.dem_path, x, y)
    assert want is not None
    assert np.allclose(dlg.point("B"), [x, y, want], atol=1e-9)
    # C: a click outside the DEM fills nothing and says why
    dlg.rows["C"]["b2d"].click()
    win.ortho_view.orient_clicked.emit(float(x) + 1e5, float(y) + 1e5)
    qapp.processEvents()
    assert dlg.point("C") is None
    assert "Nessun DEM in quel punto" in dlg.status.text()
    assert "°" in dlg.summary.text()  # A and B: the horizontal through A


def _meta_text(ch) -> str:
    return json.dumps(ch.meta, sort_keys=True, default=str)


def test_apply_and_undo_redo(win, qapp):
    from pyarchinit_photogrammetry.engine import orient
    from pyarchinit_photogrammetry.gnss import heights as hg

    ch = win.chunk
    win._store_drawings()  # what the window holds, in the chunk and in the sections file: the state before
    win._save_sections()
    t_before = np.array(ch.transform, float)
    meta_before = _meta_text(ch)
    markers_before = [m.to_dict() for m in ch.markers]
    sections = hg._sections_file(ch)
    sections_before = sections.read_bytes()
    units_before = [u.to_dict() for u in win.units]
    dlg = _open(win, qapp)
    points = _pick_three(win, qapp, dlg)
    dlg.b_apply.click()
    win.runner.wait()
    qapp.processEvents()
    want = orient.motion(orient.frame_from_points(*points)) @ t_before
    assert np.abs(np.asarray(ch.transform) - want).max() < 1e-6
    assert ch.meta["orientation"]["method"] == "points"
    assert [m.label for m in ch.markers if m.orientation] == list(orient.MARKER_LABELS)
    # the result said, with *Ricostruisci i prodotti da rifare* offered (the DEM and the orthomosaic are stale)
    assert any(kind == "choice" for kind, _t in win.said), win.said
    assert win.history.undo_text() == "Orienta il sistema locale"
    units_after = [u.to_dict() for u in win.units]
    assert units_after != units_before  # moved with the chunk
    assert win.history.undo() == "Orienta il sistema locale"
    qapp.processEvents()
    assert np.abs(np.asarray(ch.transform) - t_before).max() < 1e-12
    assert _meta_text(ch) == meta_before
    assert [m.to_dict() for m in ch.markers] == markers_before
    assert sections.read_bytes() == sections_before
    assert [u.to_dict() for u in win.units] == units_before
    assert win.history.redo() == "Orienta il sistema locale"
    qapp.processEvents()
    assert np.abs(np.asarray(ch.transform) - want).max() < 1e-6
    assert [m.label for m in ch.markers if m.orientation] == list(orient.MARKER_LABELS)
    assert [u.to_dict() for u in win.units] == units_after
    assert win.history.undo() == "Orienta il sistema locale"  # and back again
    assert np.abs(np.asarray(ch.transform) - t_before).max() < 1e-12


def test_refusal_shown_for_a_projected_chunk(win, qapp):
    win.chunk.crs = "EPSG:3004"
    win.orient_local()
    qapp.processEvents()
    assert getattr(win, "_orient_dialog", None) is None
    warned = [t for kind, t in win.said if kind == "warn"]
    assert warned and "Il chunk è in un sistema di coordinate" in warned[0]


def test_distance_disabled_with_control_bars(win, qapp):
    from pyarchinit_photogrammetry.gui.i18n import tr

    ch = win.chunk
    placed = [m for m in ch.markers if m.position is not None]
    a, b = placed[0], placed[1]
    d = float(np.linalg.norm(np.asarray(a.position) - np.asarray(b.position)))
    ch.add_scalebar(a, b, d, 0.001)
    dlg = _open(win, qapp)
    assert not dlg.distance_check.isEnabled() and not dlg.distance.isEnabled()
    assert dlg.distance_check.toolTip() == tr("Le barre di scala di controllo danno già la scala")


def test_lasso_plane(win, qapp, monkeypatch):
    from orient_helpers import tilt_motion

    from pyarchinit_photogrammetry.core import geometry as geo
    from pyarchinit_photogrammetry.engine import orient

    ch = win.chunk
    mv = win.model_view
    mv.set_chunk(ch, "point_cloud")
    qapp.processEvents()
    xyz = np.asarray(ch.point_cloud.xyz, float)
    t0 = np.linalg.inv(tilt_motion()) @ np.asarray(ch.transform, float)  # the true frame of the trench
    true = geo.apply(t0, xyz)
    floor = (true[:, 0] > 1.1) & (true[:, 0] < 1.6) & (true[:, 1] > 1.0) & (true[:, 1] < 2.0)
    assert floor.sum() > 100
    monkeypatch.setattr(mv, "select_points", lambda polygon, project, *a, **k: (floor.copy(), floor.copy()))
    dlg = _open(win, qapp)
    dlg.plane_radio.setChecked(True)
    assert not dlg.b_apply.isEnabled()  # no area yet
    dlg.b_lasso.click()
    mv._lasso_finished([(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)])
    qapp.processEvents()
    assert dlg.plane_xyz is not None and len(dlg.plane_xyz) == int(floor.sum())
    text = dlg.summary.text()
    assert "Inclinazione" in text and "°" in text
    assert dlg.b_apply.isEnabled()
    dlg.b_apply.click()
    win.runner.wait()
    qapp.processEvents()
    assert ch.meta["orientation"]["method"] == "plane"
    _c, n, _nmad = orient.fit_plane(ch.to_world(xyz[floor]))  # the floor is now the horizontal
    assert abs(abs(n[2]) - 1.0) < 1e-6


def test_undo_and_redo_mark_the_project_modified(win, qapp):
    """Undo and redo write the side files back (sections, elevations, proxies) while the saved .ppg stays as it was:
    the project is to be saved again; the proxies' files follow every step."""
    ch = win.chunk
    proxy = ch.data_dir / ch.meta["proxies"][0]["file"]
    proxy_before = proxy.read_bytes()
    dlg = _open(win, qapp)
    _pick_three(win, qapp, dlg)
    dlg.b_apply.click()
    win.runner.wait()
    qapp.processEvents()
    assert not win.isWindowModified()  # saved right after the orientation
    proxy_after = proxy.read_bytes()
    assert proxy_after != proxy_before
    win.history.undo()
    assert win.isWindowModified()
    assert proxy.read_bytes() == proxy_before
    assert win.save_document()
    assert not win.isWindowModified()
    win.history.redo()
    assert win.isWindowModified()
    assert proxy.read_bytes() == proxy_after
    win.history.undo()
    assert proxy.read_bytes() == proxy_before


def test_dialog_closed_when_another_chunk_is_active(win, qapp):
    dlg = _open(win, qapp)
    _pick_three(win, qapp, dlg)
    assert win.model_view.sd.axes is not None
    other = win.doc.add_chunk("altro")
    win._activate_chunk(other)
    qapp.processEvents()
    assert win._orient_dialog is None
    assert win.model_view._axes is None
    # a request still on its way for the chunk left is refused, the active chunk untouched
    t = np.array(other.transform, float)
    win._orient_apply({"points": [[0, 0, 0], [1, 0, 0]]}, dlg.chunk)
    win.runner.wait()
    assert np.array_equal(np.asarray(other.transform, float), t)
    assert any(kind == "warn" and "chunk attivo" in text for kind, text in win.said)


def test_closing_ends_a_pending_pick_or_lasso(win, qapp):
    mv = win.model_view
    dlg = _open(win, qapp)
    assert dlg.rows["A"]["b3d"].isEnabled()
    dlg.rows["A"]["b3d"].click()
    assert mv.stack.currentWidget() is mv.picker
    mv._pick_finished(None)  # Esc: nothing filled, the status line cleared
    assert dlg.point("A") is None and dlg.status.text() == ""
    dlg.rows["A"]["b3d"].click()
    dlg.reject()
    qapp.processEvents()
    assert mv.stack.currentWidget() is mv.canvas
    assert mv._purpose not in ("orient_p", "orient_p2", "orient_p3")
    mv.set_chunk(win.chunk, "point_cloud")
    dlg = _open(win, qapp)
    dlg.plane_radio.setChecked(True)
    dlg.b_lasso.click()
    assert mv.stack.currentWidget() is mv.lasso
    dlg.reject()
    qapp.processEvents()
    assert mv.stack.currentWidget() is mv.canvas
    assert mv._lasso_purpose == ""
    mv.start_lasso()  # the user's own lasso afterwards goes to the selection, not to the closed dialog
    assert mv._lasso_purpose == ""


def test_listeners_told_once_in_the_interface_thread(win, qapp):
    import threading

    from pyarchinit_photogrammetry.engine import orient

    seen = []
    stop = orient.on_transform_changed(lambda chunk, m: seen.append((chunk, threading.current_thread())))
    try:
        dlg = _open(win, qapp)
        _pick_three(win, qapp, dlg)
        dlg.b_apply.click()
        win.runner.wait()
        qapp.processEvents()
        assert len(seen) == 1 and seen[0][0] is win.chunk
        assert seen[0][1] is threading.main_thread()
        win.history.undo()
        assert len(seen) == 2 and seen[1][1] is threading.main_thread()
    finally:
        stop()


def test_axes_follow_the_exaggeration(win, qapp):
    mv = win.model_view
    dlg = _open(win, qapp)
    _pick_three(win, qapp, dlg)
    base = mv.base_sd.axes
    mv.set_exaggeration(3.0)
    shown = mv.sd.axes
    assert shown is not None
    assert np.allclose(shown[0], mv.sd.to_display(np.asarray(base[0])[None])[0])
    assert np.allclose(shown[1], base[1])
    mv.set_exaggeration(1.0)


def _apply_orientation(win, qapp):
    dlg = _open(win, qapp)
    _pick_three(win, qapp, dlg)
    dlg.b_apply.click()
    win.runner.wait()
    qapp.processEvents()
    assert win.history.undo_text() == "Orienta il sistema locale"


def test_undo_refused_after_the_stale_products_are_rebuilt(win, qapp):
    """Orient, rebuild the DEM in the new frame outside *Modifica > Annulla* (the DEM step of the window), Annulla:
    the step is refused with a message and leaves the history; the chunk stays as it is (the rebuilt DEM current in
    the oriented frame, not made current in the old one)."""
    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    _apply_orientation(win, qapp)
    assert stamps.is_stale(ch, "dem")
    oriented = np.array(ch.transform, float)
    win._rebuild_product(ch, "dem", lambda *a, **k: None)
    win.history.rebase()
    assert not stamps.is_stale(ch, "dem")
    dem = ch.product_path("dem").read_bytes()
    markers = [m.to_dict() for m in ch.markers]
    assert win.history.can_undo()
    win.undo()
    qapp.processEvents()
    assert np.abs(np.asarray(ch.transform) - oriented).max() == 0
    assert ch.product_path("dem").read_bytes() == dem and not stamps.is_stale(ch, "dem")
    assert [m.to_dict() for m in ch.markers] == markers
    assert not win.history.can_undo() or win.history.undo_text() != "Orienta il sistema locale"
    assert "orientamento non si può più annullare" in win.statusBar().currentMessage()


def test_rebuild_undone_then_the_orientation(win, qapp):
    """*Ricostruisci i prodotti da rifare* is a step of its own: Annulla takes the rebuilt DEM back (stale again),
    then the orientation can be undone too."""
    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    before = np.array(ch.transform, float)
    _apply_orientation(win, qapp)
    items = [x for x in stamps.stale_products(ch) if x["product"] == "dem"]
    assert win.rebuild_stale(ch, items)
    win.runner.wait()
    qapp.processEvents()
    assert not stamps.is_stale(ch, "dem")
    assert win.history.undo_text() == "Ricostruisci i prodotti da rifare"
    win.undo()
    qapp.processEvents()
    assert stamps.is_stale(ch, "dem")
    assert win.history.undo_text() == "Orienta il sistema locale"
    win.undo()
    qapp.processEvents()
    assert np.allclose(np.asarray(ch.transform, float), before, atol=1e-9)
    assert not stamps.is_stale(ch, "dem")


def test_undo_refused_after_the_georeference_is_fitted_again(win, qapp):
    """Orient, then a new alignment and *Aggiorna georeferenziazione* (the transform fitted again): Annulla is
    refused and the chunk keeps the fitted transform."""
    from orient_helpers import new_alignment

    ch = win.chunk
    _apply_orientation(win, qapp)
    new_alignment(ch)
    for m in ch.markers:
        m.position = None
    ch.update_transform()
    fitted = np.array(ch.transform, float)
    win.history.rebase()
    win.undo()
    qapp.processEvents()
    assert np.abs(np.asarray(ch.transform) - fitted).max() == 0
    assert "orientamento non si può più annullare" in win.statusBar().currentMessage()


def test_raster_pick_refused_on_a_stale_raster_or_an_elevation(win, qapp):
    """Re-orienting before the rebuild: the orthophoto pick is refused on the DEM still in the old frame (the 3D pick
    is the way), and on an elevation shown (its u, v are not the plan's x, y)."""
    from orient_helpers import centre

    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    _apply_orientation(win, qapp)
    assert stamps.is_stale(ch, "dem")
    win._show_raster("dem")
    qapp.processEvents()
    dlg = _open(win, qapp)
    dlg.rows["A"]["b2d"].click()
    qapp.processEvents()
    assert win.ortho_view.mode != "orient_pick"
    assert "va rifatto" in dlg.status.text()
    # a click arriving anyway (the pick was armed before) takes no height from the stale DEM
    dlg._pending_row = "A"
    x, y, _z = centre(ch)
    win.ortho_view.orient_clicked.emit(float(x), float(y))
    qapp.processEvents()
    assert dlg.point("A") is None and "va rifatto" in dlg.status.text()
    dlg.close()


def test_raster_pick_refused_on_an_elevation(win, qapp):
    dlg = _open(win, qapp)
    entry = next(e for e in win.chunk.meta.get("elevations") or [])
    win.show_elevation(entry)
    qapp.processEvents()
    assert win.drawings_frame is not None
    dlg.rows["A"]["b2d"].click()
    qapp.processEvents()
    assert win.ortho_view.mode != "orient_pick"
    assert "pianta" in dlg.status.text()


def test_plane_area_refused_on_an_elevation(win, qapp):
    """*Disegna l'area sull'ortofoto* of the fitted plane: on an elevation shown its u, v are not x, y: refused."""
    dlg = _open(win, qapp)
    entry = next(e for e in win.chunk.meta.get("elevations") or [])
    win.show_elevation(entry)
    qapp.processEvents()
    dlg.plane_radio.setChecked(True)
    qapp.processEvents()
    assert dlg.b_area.isEnabled()
    dlg.b_area.click()
    qapp.processEvents()
    assert not win.ortho_view.mode.startswith("req_")
    assert "pianta" in dlg.status.text()
