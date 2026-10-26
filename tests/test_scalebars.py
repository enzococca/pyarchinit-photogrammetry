"""Scale bars, as in Metashape: two markers and the distance between them scale the chunk - in
local coordinates for an object or a trench without control points - with the error of every
bar; check bars only measure, several control bars are averaged."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import crs as crs_mod
from test_chunks import _gcp, _true_centres, doc, two_chunks  # noqa: F401 - chunks aligned on their own


def _true(scene, a, b) -> float:
    return float(np.linalg.norm(_gcp(scene, a.label) - _gcp(scene, b.label)))


def _pairs(chunk, scene):
    """Pairs of markers seen in the chunk, the farthest first."""
    chunk.triangulate_markers()
    ms = [m for m in chunk.markers if m.position is not None]
    assert len(ms) >= 4
    return sorted(((a, b) for i, a in enumerate(ms) for b in ms[i + 1:]), key=lambda p: -_true(scene, *p))


def test_scale_bars_in_local_coordinates(doc, scene):
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), checks = pairs[0], pairs[1:5]
    bar = ch.add_scalebar(a.label, str(b.code), _true(scene, a, b))  # by label or by target code
    assert bar.label == f"{a.label}_{b.label}" and not bar.checkpoint
    for x, y in checks:
        ch.add_scalebar(x, y, _true(scene, x, y), checkpoint=True)
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    errors = {e["label"]: e for e in rep["scalebars"]}
    assert abs(errors[bar.label]["error"]) < 1e-9  # one control bar: its length exactly
    checked = [e for e in rep["scalebars"] if e["type"] == "check"]
    assert len(checked) == 4 and max(abs(e["error"]) for e in checked) < 0.004
    assert rep["scalebar_check_rmse"] < 0.003
    # a check bar never moves the scale, even a wrong one
    s = ch.scale
    x, y = checks[0]
    ch.add_scalebar(x, y, 10.0, checkpoint=True)  # the same pair: the bar changes, no new one
    assert len(ch.scalebars) == 5
    ch.update_transform()
    assert ch.scale == pytest.approx(s)
    # two control bars: their weighted mean, within the error of the photogrammetry
    ch.add_scalebar(x, y, _true(scene, x, y))
    rep = ch.update_transform()
    assert abs(ch.scale / s - 1) < 0.002 and rep["scalebar_control_rmse"] < 0.003
    ch.remove_scalebar(bar.label)
    assert bar.label not in {sb.label for sb in ch.scalebars}


def test_the_gps_of_the_photos_does_not_move_an_object_or_a_trench(doc, scene):
    """Photos with the position of a phone or a drone (5 m accuracy) over a few metres: the
    scale bars scale the chunk in local coordinates instead of placing it with that noise."""
    ch = doc.chunks[0]
    (a, b) = _pairs(ch, scene)[0]
    truth = _true_centres(scene)
    rng = np.random.default_rng(0)
    for cam in ch.aligned_cameras:
        cam.reference.location = list(truth[cam.label] + rng.normal(0, 3.0, 3))
        cam.reference.accuracy, cam.reference.enabled = [5.0, 5.0, 10.0], True
        cam.meta["reference_crs"] = "EPSG:3004"
    ch.add_scalebar(a, b, _true(scene, a, b))
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    assert abs(rep["scalebars"][0]["error"]) < 1e-9
    rep = ch.update_transform(use_cameras=True)  # asked for: the photos place it, the bar scales it
    assert rep["method"] == "cameras+scalebars" and ch.crs == "EPSG:3004"
    rep = ch.update_transform()  # and back: local coordinates are not labelled with the CRS of the photos
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    assert abs(rep["scalebars"][0]["error"]) < 1e-9


def test_bars_give_the_scale_to_gnss_cameras(doc, scene):
    """Precise camera positions (RTK) with a scale 3 % off: the cameras place and turn the
    survey, a control bar measured to the millimetre outweighs them on the scale (each by its
    accuracy), a check bar measures it."""
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), (x, y) = pairs[0], pairs[1]
    truth = _true_centres(scene)
    centre = np.mean(list(truth.values()), axis=0)
    rng = np.random.default_rng(1)
    for cam in ch.aligned_cameras:
        t = truth[cam.label]
        cam.reference.location = list(centre + 1.03 * (t - centre) + rng.normal(0, 0.003, 3))
        cam.reference.accuracy, cam.reference.enabled = [0.02, 0.02, 0.03], True
        cam.meta["reference_crs"] = "EPSG:3004"
    check = ch.add_scalebar(x, y, _true(scene, x, y), checkpoint=True)
    rep = ch.update_transform()
    assert rep["method"] == "cameras"
    error = {e["label"]: e["error"] for e in rep["scalebars"]}[check.label]
    assert abs(error) > 0.015  # 3 % of the bar
    ch.add_scalebar(a, b, _true(scene, a, b))
    rep = ch.update_transform()
    assert rep["method"] == "cameras+scalebars" and ch.crs == "EPSG:3004"
    assert abs({e["label"]: e["error"] for e in rep["scalebars"]}[check.label]) < 0.004
    # the survey stays where the cameras put it
    ends = np.array([ch.to_world(m.position) for m in (a, b)])
    assert np.linalg.norm(ends.mean(0) - np.array([_gcp(scene, m.label) for m in (a, b)]).mean(0)) < 0.1


def test_bars_and_control_points_weigh_by_their_accuracy(doc, scene):
    """With control points the bars join the fit, as in Metashape's reference adjustment: a
    precise bar corrects the scale of loose points, a loose bar does not move precise ones."""
    ch = doc.chunks[0]
    (a, b), (x, y) = _pairs(ch, scene)[:2]
    gcps = [m for m in ch.markers if m.position is not None]
    truth = {m.label: _gcp(scene, m.label) for m in gcps}
    centre = np.mean(list(truth.values()), axis=0)
    for m in gcps:  # points surveyed with a scale 2 % off, 5 cm accuracy
        m.reference.location = list(centre + 1.02 * (truth[m.label] - centre))
        m.reference.accuracy, m.reference.enabled = [0.05, 0.05, 0.05], True
    check = ch.add_scalebar(x, y, _true(scene, x, y), checkpoint=True)
    rep = ch.update_transform()
    assert rep["method"] == "markers"

    def error(r):
        return {e["label"]: e["error"] for e in r["scalebars"]}[check.label]

    assert abs(error(rep)) > 0.015 * _true(scene, x, y)
    bar = ch.add_scalebar(a, b, _true(scene, a, b), accuracy=0.0005)
    rep = ch.update_transform()
    assert rep["method"] == "markers+scalebars" and abs(error(rep)) < 0.003
    # precise points, a loose bar 3 % off: the points keep their scale
    for m in gcps:
        m.reference.location, m.reference.accuracy = list(truth[m.label]), [0.001] * 3
    ch.remove_scalebar(bar.label)
    ch.update_transform()
    s = ch.scale
    ch.add_scalebar(a, b, 1.03 * _true(scene, a, b), accuracy=0.05)
    rep = ch.update_transform()
    assert rep["method"] == "markers+scalebars" and abs(ch.scale / s - 1) < 0.001
    assert rep["control_rmse"] < 0.003


def test_joint_fit_of_points_and_bars():
    """The scale of the joint fit: Umeyama's without bars, the bars' when they weigh far more."""
    from pyarchinit_photogrammetry.core import geometry
    from pyarchinit_photogrammetry.markers.gcp import similarity_with_bars

    rng = np.random.default_rng(3)
    src = rng.normal(0, 1, (8, 3))
    truth = geometry.compose_similarity(2.0, geometry.rotation_from_euler_ypr(30.0, -10.0, 5.0),
                                        np.array([10.0, 20.0, 5.0]))
    dst = geometry.apply(truth, src) + rng.normal(0, 0.01, (8, 3))
    w = np.full(8, 1 / 0.01 ** 2)
    plain = geometry.decompose_similarity(geometry.umeyama(src, dst, weights=w))[0]
    joint = geometry.decompose_similarity(similarity_with_bars(src, dst, w, [1.0], [2.5], [1e-12]))[0]
    assert joint == pytest.approx(plain, rel=1e-9)
    joint = geometry.decompose_similarity(similarity_with_bars(src, dst, w, [1.0], [2.5], [1e12]))[0]
    assert joint == pytest.approx(2.5, rel=1e-6)


def test_a_marker_in_one_photo_lies_on_the_surface(processed):
    """A marker measured in one photo only is placed where its ray meets the surface (the dense
    cloud, else the tie points), as Metashape places it on the model: it gets coordinates and can
    end a scale bar. On the trench: within a few millimetres of the surveyed target."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    doc, _ch, _ = processed
    ch = ppg.Document.open(doc.path).chunk  # in memory: nothing is saved
    for use_cloud in (True, False):
        if not use_cloud:
            ch.products.pop("point_cloud")  # the tie points become the surface
        for m in ch.markers:
            truth = np.asarray(m.reference.location, float)
            saved = m.projections
            m.projections = {k: saved[k] for k in list(saved)[:1]}
            ch.triangulate_markers()
            assert gcp.views_of(ch, m) == 1 and m.position is not None
            assert np.linalg.norm(ch.to_world(m.position[None])[0] - truth) < 0.003
            m.projections = saved
    ch.triangulate_markers()


def test_a_marker_placed_in_3d_is_seen_in_the_photos(processed):
    """A marker added on the model, the cloud or the orthophoto is projected on the photos that
    see it (not pinned, as Metashape's), so it is triangulated like the others: on the trench its
    projections fall within a pixel of where the target was detected."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    doc, _ch, _ = processed
    ch = ppg.Document.open(doc.path).chunk
    target = ch.markers[0]
    truth = ch.to_internal(np.asarray([target.reference.location], float))[0]
    m = ch.add_marker("point 1")
    n = gcp.project_marker(ch, m, truth)
    assert n >= 2 and not any(p.pinned for p in m.projections.values())
    common = set(m.projections) & set(target.projections)
    assert len(common) >= 2
    for k in common:
        a, b = m.projections[k], target.projections[k]
        assert np.hypot(a.x - b.x, a.y - b.y) < 1.0
    ch.triangulate_markers()
    assert np.linalg.norm(ch.to_world(m.position[None])[0] - target.reference.location) < 0.003
    look = np.mean([c.transform[:3, 2] for c in ch.aligned_cameras], axis=0)  # where the photos look
    side = np.cross(look, [1.0, 0.0, 0.0] if abs(look[0]) < 0.9 else [0.0, 1.0, 0.0])
    span = float(np.linalg.norm(np.ptp([c.transform[:3, 3] for c in ch.aligned_cameras], axis=0)))
    far = truth + 100 * span * side / np.linalg.norm(side)  # far to the side: no photo frames it
    lone = ch.add_marker("point 2", position=far)
    assert gcp.project_marker(ch, lone, lone.position) == 0
    ch.triangulate_markers()
    assert np.allclose(lone.position, far)  # kept


def test_a_georeferenced_chunk_is_scaled_where_it_is(processed):
    """A chunk georeferenced elsewhere (an imported project) with no reference to fit it again:
    a control bar scales it about the bar, in its CRS, instead of moving it to local coordinates."""
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    ch = ppg.Document.open(doc.path).chunk
    for m in ch.markers:
        m.reference.enabled = False
    for c in ch.cameras:
        c.reference.enabled = False
    crs, a, b = ch.crs, ch.markers[0], ch.markers[1]
    ends = ch.to_world(np.array([a.position, b.position]))
    measured = float(np.linalg.norm(ends[0] - ends[1]))
    ch.add_scalebar(a, b, 0.9 * measured)
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and ch.crs == crs  # still in its CRS
    now = ch.to_world(np.array([a.position, b.position]))
    assert abs(float(np.linalg.norm(now[0] - now[1])) - 0.9 * measured) < 1e-6
    assert np.allclose(now.mean(axis=0), ends.mean(axis=0), atol=1e-6)  # about the bar


def test_scale_bars_from_a_file(doc, scene, tmp_path):
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), (c, d) = pairs[0], pairs[1]
    f = tmp_path / "barre.csv"
    f.write_text("marker_a;marker_b;distanza\n"  # semicolons, Italian decimal commas, codes or labels
                 f"{a.code};{b.code};{_true(scene, a, b):.5f}".replace(".", ",") + "\n"
                 f"{c.label};{d.label};{_true(scene, c, d):.5f};0,002;check\n"
                 "# a comment\n"
                 "98;99;1,0\n", encoding="utf-8")
    out = ch.import_scalebars(f)
    assert len(out["bars"]) == 2 and out["missing"] == ["98-99"]
    first, second = ch.scalebar(out["bars"][0]), ch.scalebar(out["bars"][1])
    assert first.distance == pytest.approx(_true(scene, a, b), abs=1e-5) and not first.checkpoint
    assert second.checkpoint and second.accuracy == pytest.approx(0.002)
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and rep["scalebar_check_rmse"] < 0.004


def test_printable_scale_bar(tmp_path):
    """Two coded targets at a known distance, a ruler, and the CSV to import."""
    import cv2

    from pyarchinit_photogrammetry.markers import targets

    files, csv = targets.generate_scale_bar(tmp_path / "barra.png", distance_mm=150.0, count=2)
    assert len(files) == 2
    (a, b), (c, d) = targets.scale_bar_codes(count=2)
    assert csv.read_text().splitlines() == ["marker_a,marker_b,distance", f"{a},{b},0.15000", f"{c},{d},0.15000"]
    found = {t["code"]: t["center"] for t in targets.detect(cv2.imread(str(files[0])))}
    assert set(found) == {a, b}
    assert np.linalg.norm(found[a] - found[b]) / (300 / 25.4) == pytest.approx(150.0, abs=0.2)
    with pytest.raises(ValueError):
        targets.generate_scale_bar(tmp_path / "troppo.png", distance_mm=400.0)


def test_command_line(two_chunks, scene, tmp_path):
    """ppg scalebar: a bar by target codes, a check bar, the list with the errors, and away."""
    import shutil

    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    d = tmp_path / "copia"
    shutil.copytree(two_chunks.parent, d)
    project = d / two_chunks.name
    ch = ppg.Document.open(project).chunks[0]
    (a, b), (x, y) = _pairs(ch, scene)[:2]
    run = CliRunner()

    def ok(*args):
        r = run.invoke(app, list(args))
        assert r.exit_code == 0, r.output + repr(r.exception)
        return r.output

    out = ok("scalebar", str(project), "--markers", f"{a.code},{b.code}", "--distance", f"{_true(scene, a, b):.5f}",
             "--chunk", ch.label)
    assert "method scalebars, CRS LOCAL" in out
    out = ok("scalebar", str(project), "--markers", f"{x.label},{y.label}", "--distance", f"{_true(scene, x, y):.5f}",
             "--check", "--chunk", ch.label)
    line = next(ln for ln in out.splitlines() if ln.startswith(f"{x.label}_{y.label}"))
    assert ", check, error " in line and abs(float(line.split("error ")[1].split(" mm")[0])) < 4
    ok("scalebar", str(project), "--remove", "all", "--chunk", ch.label)
    assert ppg.Document.open(project).chunks[0].scalebars == []


def test_scale_bars_in_the_interface(two_chunks, scene, qapp_bars, monkeypatch, tmp_path):
    """Riferimenti: two markers selected, *Crea barra di scala*, the distance; Aggiorna scales the
    survey in local coordinates; a check bar edited in the table; the bars in the 3D view."""
    from PySide6.QtCore import QItemSelectionModel, QSettings

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.scene import build_scene

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc = ppg.Document.open(two_chunks)
    monkeypatch.setattr(doc, "save", lambda *a, **k: None)  # the shared project of the module stays as it is
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.show()
    qapp_bars.processEvents()
    ch = w.chunk
    (a, b), (x, y) = _pairs(ch, scene)[:2]
    pane = w.reference
    pane.set_chunk(ch)
    sel = pane.table.selectionModel()
    for r, m in enumerate(ch.markers):
        if m.key in (a.key, b.key):
            sel.select(pane.table.model().index(r, 1),
                       QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    assert sorted(pane.selected_markers()) == sorted([a.key, b.key])
    seen, answers = [], {}

    def ask(cls, parent, title, fields, intro=""):
        seen.append(({f.name: f for f in fields}, intro))
        return dict(answers)

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    answers.update(a=a.key, b=b.key, distance=_true(scene, a, b), accuracy=0.001, check=False, update=True)
    pane.scalebar_requested.emit(pane.selected_markers())  # the context menu of the two markers
    w.runner.wait()
    qapp_bars.processEvents()
    fields, intro = seen[-1]
    assert {fields["a"].default, fields["b"].default} == {a.key, b.key} and "unita' arbitrarie" in intro
    assert len(ch.scalebars) == 1 and w.last_report["method"] == "scalebars"
    assert pane.bars.rowCount() == 1 and pane.bars.item(0, 8).text()  # its error
    answers.update(a=x.key, b=y.key, distance=_true(scene, x, y), check=True)
    pane.scalebar_requested.emit([x.key, y.key])
    w.runner.wait()
    qapp_bars.processEvents()
    assert pane.bars.rowCount() == 2 and "RMSE barre check" in pane.summary.text()
    check = next(e for e in w.last_report["scalebars"] if e["type"] == "check")
    assert abs(check["error"]) < 0.004
    # a new distance typed in the table: the error follows at once
    pane.bars.item(1, 4).setText(f"{_true(scene, x, y) + 0.1:.4f}".replace(".", ","))
    qapp_bars.processEvents()
    assert ch.scalebars[1].distance == pytest.approx(_true(scene, x, y) + 0.1, abs=1e-4)
    assert float(pane.bars.item(1, 8).text().replace(",", ".")) == pytest.approx(check["error"] - 0.1, abs=2e-4)
    pane.bars.item(1, 4).setText("abc")  # not a length: the distance of the bar comes back
    qapp_bars.processEvents()
    assert float(pane.bars.item(1, 4).text().replace(",", ".")) == pytest.approx(ch.scalebars[1].distance, abs=1e-4)
    sd = build_scene(ch, "tie_points")
    assert len(sd.scalebars) == 2 and [s[3] for s in sd.scalebars] == [False, True]
    pane.delete_bars([1])
    assert len(ch.scalebars) == 1 and pane.bars.rowCount() == 1
    w.setWindowModified(False)
    w.photos.stop()
    w.close()


def test_optimize_with_scale_bars_in_the_interface(two_chunks, scene, qapp_bars, monkeypatch, tmp_path):
    """*Ottimizza camere* with two control bars: the dialog offers the bars, the adjustment holds
    them and the log reports their errors before and after."""
    import shutil

    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    copy = tmp_path / "copia"
    shutil.copytree(two_chunks.parent, copy)  # the adjustment writes the model of the chunk
    doc = ppg.Document.open(copy / two_chunks.name)
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    ch = w.chunk
    pairs = _pairs(ch, scene)
    a, b = pairs[0]
    x, y = next(p for p in pairs if not {p[0].key, p[1].key} & {a.key, b.key})  # two bars without common ends
    ch.add_scalebar(a, b, _true(scene, a, b))
    ch.add_scalebar(x, y, _true(scene, x, y))
    seen = []

    def ask(cls, parent, title, fields, intro=""):
        seen.append(({f.name: f for f in fields}, intro))
        return {"markers": True, "weight": 4, "pp": False}

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    w.optimize_cameras()
    w.runner.wait()
    qapp_bars.processEvents()
    fields, intro = seen[-1]
    assert "2 barre" in fields["markers"].label and fields["markers"].default is True and "barre di scala" in intro
    assert ch.meta["sfm"]["optimize"]["scale_bars"] == 2
    log = " ".join(e[2] for e in w.log_pane.entries)
    assert "2 barre di scala: errore" in log
    assert w.last_report["method"] == "scalebars"
    w.setWindowModified(False)
    w.photos.stop()
    w.close()


def test_markers_on_the_orthophoto_and_the_model(processed, qapp_bars, monkeypatch, tmp_path):
    """Markers placed as in Metashape without a photo: a click with the Marker tool on the
    orthophoto (height from the DEM) and a click on the dense cloud in the 3D view. Both are
    projected on the photos that see them, show their estimated X, Y, Z in Riferimenti and are
    drawn on the orthophoto; a scale bar between them measures their distance."""
    import shutil

    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.markers import gcp

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc0, _ch, _ = processed
    copy = tmp_path / "copia"
    copy.mkdir()
    shutil.copy2(doc0.path, copy / doc0.path.name)
    shutil.copytree(doc0.files_dir, copy / doc0.files_dir.name)
    doc = ppg.Document.open(copy / doc0.path.name)
    ch = doc.chunk
    truth = {m.label: np.asarray(m.reference.location, float) for m in ch.markers}
    ch.markers = []  # no marker yet: they are placed here
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp_bars.processEvents()
    w._show_raster("ortho")
    qapp_bars.processEvents()
    a, b = truth["target 0"], truth["target 4"]
    w.set_draw_mode("marker")
    w.ortho_view.marker_clicked.emit(float(a[0]), float(a[1]))
    qapp_bars.processEvents()
    m1 = ch.marker("point 1")
    assert gcp.views_of(ch, m1, estimated=True) >= 2 and np.linalg.norm(ch.to_world(m1.position[None])[0] - a) < 0.006
    # the 3D view: a click where target 4 is on the screen
    mv = w.model_view
    w.show_view(mv)
    mv.show_kind("point_cloud")
    qapp_bars.processEvents()
    image, project = mv.canvas.snapshot()
    x, y, _z, ok = project((b - mv.sd.offset)[None])
    assert ok[0]
    point = mv.pick_point(float(x[0]), float(y[0]), project, mv.canvas.view_ray)
    assert point is not None and np.linalg.norm(ch.to_world(point[None])[0] - b) < 0.01
    mv._project, mv._ray = project, mv.canvas.view_ray
    mv._pick_finished((float(x[0]), float(y[0])))  # as a click on the frozen frame
    qapp_bars.processEvents()
    m2 = ch.marker("point 2")
    assert gcp.views_of(ch, m2, estimated=True) >= 2 and np.linalg.norm(ch.to_world(m2.position[None])[0] - b) < 0.01
    # Riferimenti shows their estimated coordinates (there are no reference ones)
    pane = w.reference
    pane.set_chunk(ch)
    assert pane.b_est.isChecked() and pane.table.horizontalHeaderItem(3).text() == "X stimata"
    assert float(pane.table.item(0, 3).text()) == pytest.approx(ch.to_world(m1.position[None])[0][0], abs=1e-3)
    assert {f["marker"] for f in w.ortho_view.features("Marker")} == {"point 1", "point 2"}
    bar = ch.add_scalebar(m1, m2, float(np.linalg.norm(a - b)), checkpoint=True)
    e = next(r for r in gcp.scalebar_errors(ch) if r["label"] == bar.label)
    assert abs(e["error"]) < 0.01
    w.setWindowModified(False)
    w.photos.stop()
    w.close()


def test_guided_marker_placement_in_the_photos(processed, qapp_bars, tmp_path):
    """As in Metashape: a marker placed on one photo gets blue flags (estimated) on the other
    photos that see it; the photos are filtered on it, with badges; *Successiva* opens the next
    one centred on the marker; dragging a blue flag onto the point makes it green (measured) and
    the marker is triangulated; a double click confirms a flag where it is."""
    import shutil

    from PySide6.QtCore import QPoint, QSettings, Qt
    from PySide6.QtTest import QTest

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.markers import gcp

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc0, _ch, _ = processed
    copy = tmp_path / "copia"
    copy.mkdir()
    shutil.copy2(doc0.path, copy / doc0.path.name)
    shutil.copytree(doc0.files_dir, copy / doc0.files_dir.name)
    (copy / "photos").symlink_to(doc0.path.parent / "photos")  # the project names them relative to itself
    doc = ppg.Document.open(copy / doc0.path.name)
    ch = doc.chunk
    target = ch.markers[0]
    seen = dict(target.projections)  # where the target was detected: the truth in every photo
    truth = np.asarray(target.reference.location, float)
    ch.markers = []
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp_bars.processEvents()
    assert w.photos_panel.label.text() == f"Tutte le foto ({len(ch.cameras)})"  # counted once the photos are in
    first, second, third = (ch.camera(k) for k in list(seen)[:3])
    w.open_photo(first)
    m = ch.add_marker("point 1")
    w.photo_view._place(m, seen[first.key].x, seen[first.key].y)  # right click > Posiziona marker qui
    w.photo_view.marker_created.emit(m)
    qapp_bars.processEvents()
    assert gcp.views_of(ch, m) == 1 and gcp.views_of(ch, m, estimated=True) >= 2  # blue flags elsewhere
    assert np.linalg.norm(ch.to_world(m.position[None])[0] - truth) < 0.003
    shown = w.photos.visible_cameras()
    assert w.photos.filter_title == "Foto di point 1" and {first.key, second.key, third.key} <= {c.key for c in shown}
    assert w.photos._flags[second.key][:2] == (0, 1) and w.photos._flags[first.key][:2] == (1, 0)  # badges
    # the next photo of the filtered list, centred on the marker
    w.open_photo(second)
    view = w.photo_view
    assert view.camera is second and view.transform().m11() >= 1.0
    pr = m.projections[second.key]
    assert pr.pinned is None
    # drag the blue flag onto the target (a few pixels off on purpose)
    pr.x, pr.y = seen[second.key].x + 6.0, seen[second.key].y - 4.0
    view.draw_markers()
    qapp_bars.processEvents()
    start = view.mapFromScene(pr.x, pr.y) + QPoint(3, -10)  # on the pole of the flag
    end = view.mapFromScene(seen[second.key].x, seen[second.key].y) + QPoint(3, -10)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp_bars.processEvents()
    got = m.projections[second.key]
    assert got.pinned is True and np.hypot(got.x - seen[second.key].x, got.y - seen[second.key].y) < 1.5
    assert gcp.views_of(ch, m) == 2 and np.linalg.norm(ch.to_world(m.position[None])[0] - truth) < 0.003
    # a double click confirms a blue flag where it is
    w.photo_step(1)
    cam = view.camera
    if cam.key in m.projections and not m.projections[cam.key].measured:
        p3 = m.projections[cam.key]
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                          view.mapFromScene(p3.x, p3.y) + QPoint(3, -10))
        qapp_bars.processEvents()
        assert m.projections[cam.key].pinned is True
    # removed by hand from a photo: no blue flag goes back there when the marker moves
    w.open_photo(third)
    view._remove(m)
    qapp_bars.processEvents()
    assert third.key not in m.projections
    gcp.project_marker(ch, m)
    assert third.key not in m.projections and gcp.views_of(ch, m, estimated=True) >= 1
    w.photos.set_filter(None)
    assert len(w.photos.visible_cameras()) == len(ch.cameras)
    w.photos.set_chunk(None)
    assert w.photos_panel.label.text() == "Tutte le foto (0)"
    w.setWindowModified(False)
    w.photos.stop()
    w.close()


@pytest.fixture(scope="module")
def qapp_bars():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
