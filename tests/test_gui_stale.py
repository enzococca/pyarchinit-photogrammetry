"""Rasters built before the last change of the chunk's system: the 2D view says so in a banner, drawing on them is
refused (to plugins too), *Ricostruisci i prodotti da rifare* builds them again in order with the parameters each was
built with, and a kept version made in another system says so when it becomes the active one."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

BANNER = "Questo raster è stato fatto prima dell'ultimo cambio del sistema del chunk: va rifatto"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, processed, tmp_path, monkeypatch):
    from orient_helpers import close_window, window

    w = window(processed, tmp_path, monkeypatch)
    qapp.processEvents()
    yield w
    close_window(w)


def _points(ch):
    """Three points of the dense cloud (chunk coordinates) spread over the trench."""
    xyz = np.asarray(ch.point_cloud.xyz, float)
    c = np.median(xyz, axis=0)
    _, _, vt = np.linalg.svd(xyz[::50] - c, full_matrices=False)
    span = np.ptp((xyz - c) @ vt[:2].T, axis=0)
    targets = [c - 0.3 * span[0] * vt[0], c + 0.3 * span[0] * vt[0], c + 0.3 * span[1] * vt[1]]
    return ch.to_world(np.array([xyz[np.argmin(np.linalg.norm(xyz - t, axis=1))] for t in targets]))


def _orient(win, qapp, **kw):
    ch = win.chunk
    result = win.orient_chunk(ch, points=_points(ch), **kw)
    qapp.processEvents()
    return result


def test_banner_on_a_stale_raster(win, qapp):
    view = win.ortho_view
    win._show_raster("dem")
    qapp.processEvents()
    assert view.banner_text() == ""
    _orient(win, qapp)
    win._show_raster("dem")
    qapp.processEvents()
    assert win._shown_raster_stale()
    assert view.banner_text().startswith(BANNER)
    assert "Ricostruisci i prodotti da rifare" in view.banner_text()
    assert view._banner.isVisible()
    assert view._banner.width() == view.viewport().width()
    win._show_raster("ortho")  # the orthomosaic on the old DEM: stale too
    assert view.banner_text().startswith(BANNER)
    assert win.history.undo() == "Orienta il sistema locale"
    qapp.processEvents()
    win._show_raster("dem")
    assert not win._shown_raster_stale()
    assert view.banner_text() == ""
    assert not view._banner.isVisible()


def test_banner_survives_a_load_and_follows_the_elevations(win, qapp):
    view = win.ortho_view
    view.raster_loaded.disconnect(win._raster_loaded)  # the view alone: its notice stays across a load
    view.set_banner("prova")
    win._release_views()  # clear_raster
    assert view.banner_text() == "prova"
    view.raster_loaded.connect(win._raster_loaded)
    win._release_views()  # with the window: judged again (nothing shown, nothing stale)
    assert view.banner_text() == ""
    _orient(win, qapp, distance=3.0)  # a scale: the elevations' pixels no longer fit
    entry = win.chunk.meta["elevations"][0]
    win.show_elevation(entry)
    assert view.banner_text().startswith(BANNER)


def test_drawing_on_a_stale_raster_is_refused(win, qapp):
    from pyarchinit_photogrammetry.gui.main_window import STALE_REFUSED

    assert {"polygon", "line", "quota", "patch", "free_polygon"} <= set(STALE_REFUSED)
    _orient(win, qapp)
    win._show_raster("dem")
    win.said.clear()
    win.set_draw_mode("polygon")
    assert win.ortho_view.mode == "pan"
    assert win.draw_actions["pan"].isChecked()
    assert any(kind == "warn" and "va rifatto prima di disegnarci sopra" in text for kind, text in win.said)
    cancelled = []
    assert win.request_geometry("polygon", lambda xy: None, lambda: cancelled.append(1)) is False
    assert cancelled == [1]
    assert win.ortho_view.mode == "pan"
    win.set_draw_mode("measure")  # measuring and selecting stay allowed
    assert win.ortho_view.mode == "measure"
    win.set_draw_mode("select")
    assert win.ortho_view.mode == "select"
    win.set_draw_mode("pan")
    win.history.undo()
    qapp.processEvents()
    win._show_raster("dem")
    win.set_draw_mode("polygon")
    assert win.ortho_view.mode == "polygon"
    win.set_draw_mode("pan")


def test_rebuild_order_and_parameters(win, qapp, monkeypatch):
    from orient_helpers import _write_elevation

    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import elevation

    ch = win.chunk
    _orient(win, qapp, distance=3.0)
    stale = stamps.stale_products(ch)
    kinds = [(s["kind"], s["product"]) for s in stale]
    assert kinds[:2] == [("product", "dem"), ("product", "orthomosaic")]
    assert ("elevation", "elevation") in kinds
    ch.meta["dem"] = {"resolution": 0.05, "source": "point_cloud", "classes": [2], "spikes_removed": 0}
    ch.meta.setdefault("orthomosaic", {}).update(
        resolution=0.01, surface="dem", blending="average", blend_sigma=4.0, max_angle=60.0, colour_correction=False,
        ghosting=True, fill_holes=False, hidden_classes=[7])
    old = next(e for e in ch.meta["elevations"] if e["id"] == "el000001")
    old.update(blending="disabled", max_angle=55.0, ghosting=True, fill_holes=False, colour_correction=False)
    old_frame, old_bounds = dict(old["frame"]), list(old["bounds"])
    frames_before = dict(ch.meta["drawings"]["frames"]["el000001"])
    calls = []

    def fake_dem(resolution=None, source="point_cloud", interpolate=True, classes=None, progress=None):
        calls.append(("dem", {"resolution": resolution, "source": source, "interpolate": interpolate,
                              "classes": classes}))
        stamps.stamp(ch, "dem")
        return ch.dem_path

    def fake_ortho(resolution=None, surface="dem", blending="mosaic", hide_classes=None, progress=None, **kw):
        calls.append(("orthomosaic", {"resolution": resolution, "surface": surface, "blending": blending,
                                      "hide_classes": hide_classes, **kw}))
        stamps.stamp(ch, "orthomosaic")
        return ch.orthomosaic_path

    def fake_elevation(chunk, frame, resolution=None, name=None, depth=None, bounds=None, kind="elevation",
                       scale="cylinder", progress=None, **kw):
        calls.append(("elevation", {"frame": frame.to_dict(), "resolution": resolution, "name": name,
                                    "depth": depth, "bounds": list(bounds), "kind": kind, "scale": scale, **kw}))
        entry = _write_elevation(chunk, frame, "nuovo001", kind, bounds, depth, resolution)
        entry["name"] = name
        return entry

    monkeypatch.setattr(ch, "build_dem", fake_dem)
    monkeypatch.setattr(ch, "build_orthomosaic", fake_ortho)
    monkeypatch.setattr(elevation, "build_elevation", fake_elevation)
    assert win.a_rebuild_stale.isEnabled()
    win._show_raster("dem")
    assert win.ortho_view.banner_text().startswith(BANNER)
    win.rebuild_stale(ch)
    win.runner.wait()
    qapp.processEvents()
    assert [c[0] for c in calls] == ["dem", "orthomosaic", "elevation"], calls
    assert calls[0][1] == {"resolution": 0.05, "source": "point_cloud", "interpolate": True, "classes": [2]}
    o = calls[1][1]
    assert (o["resolution"], o["surface"], o["blending"], o["hide_classes"]) == (0.01, "dem", "average", [7])
    assert (o["blend_sigma"], o["max_angle"], o["colour_correction"], o["ghosting"], o["fill_holes"]) == \
        (4.0, 60.0, False, True, False)
    e = calls[2][1]
    assert e["frame"].keys() == old_frame.keys() and e["frame"]["kind"] == old_frame["kind"]
    for key in ("origin", "u_axis", "v_axis"):  # the frame read again from its record (axes normalised)
        assert np.allclose(e["frame"][key], old_frame[key], atol=1e-12)
    assert e["bounds"] == old_bounds and e["kind"] == "section"
    assert (e["resolution"], e["name"]) == (old["resolution"], old["name"])
    assert (e["blending"], e["max_angle"], e["ghosting"], e["fill_holes"], e["colour_correction"]) == \
        ("disabled", 55.0, True, False, False)
    ids = [x["id"] for x in ch.meta["elevations"]]
    assert ids.count("el000001") == 1 and "nuovo001" not in ids  # the same id, in its place
    new = next(x for x in ch.meta["elevations"] if x["id"] == "el000001")
    assert Path(new["orthophoto"]).as_posix() == "elevations/el000001/elevation.tif"  # the separator of the system
    assert (ch.data_dir / new["orthophoto"]).exists()
    assert not (ch.data_dir / "elevations" / "nuovo001").exists()
    assert not stamps.entry_stale(ch, new)
    assert ch.meta["drawings"]["frames"]["el000001"] == frames_before  # the drawings kept with it
    assert not any(s["kind"] in ("product", "elevation") for s in stamps.stale_products(ch))
    assert win.ortho_view.banner_text() == ""
    assert any(kind == "info" and "Rifatti" in text for kind, text in win.said), win.said
    assert not win.a_rebuild_stale.isEnabled()


def test_stale_version_activation_said(win, qapp):
    from pyarchinit_photogrammetry.core import versions

    ch = win.chunk
    kept = versions.keep_active(ch, "dem")  # a copy made in the system before the orientation
    _orient(win, qapp)
    from pyarchinit_photogrammetry.core import stamps

    stamps.stamp(ch, "dem")  # the active DEM rebuilt in the new system
    win.activate_version("dem", kept["id"])
    win.runner.wait()
    qapp.processEvents()
    text = win.statusBar().currentMessage()
    assert "è stata fatta in un altro sistema del chunk: va rifatta" in text, text
    assert kept["name"] in text
    assert win.ortho_view.banner_text().startswith(BANNER)


def test_rebuild_offered_after_the_orientation(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    asked, rebuilt = [], []
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: asked.append(a[4]) or 0)
    monkeypatch.setattr(win, "rebuild_stale_products", lambda chunk, items: rebuilt.append((chunk, items)))
    ch = win.chunk
    result = win.orient_chunk(ch, points=_points(ch))
    win._orient_finish(ch, {**result, "state": None})  # the box of the dialog's path (the step is pushed already)
    assert asked and asked[0][0] == "Ricostruisci i prodotti da rifare"
    assert rebuilt and rebuilt[0][0] is ch
    assert [s["product"] for s in rebuilt[0][1]][:2] == ["dem", "orthomosaic"]


def _square(ch, half=0.6):
    from orient_helpers import centre

    x, y, _z = centre(ch)
    return [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]


def test_tin_from_a_stale_dem_reads_stale(win):
    import shapely

    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import tin as T

    ch = win.chunk
    T.build_tin(ch, source="dem", boundary=shapely.Polygon(_square(ch)))
    assert not stamps.is_stale(ch, "tin")
    saved = ch.meta["tin"]
    assert saved["classes"] is None
    assert np.allclose(np.asarray(saved["boundary"])[:, :2], _square(ch))  # the boundary it was built with, kept
    ch.meta["built_in"]["dem"] = np.eye(4).tolist()  # the DEM of another system
    T.build_tin(ch, source="dem")
    assert stamps.is_stale(ch, "tin")  # in the DEM's coordinates: stale too
    assert "boundary" not in ch.meta["tin"]


def test_tin_left_by_hand_when_the_dem_stays_stale(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import tin as T

    ch = win.chunk
    T.build_tin(ch, source="dem")
    _orient(win, qapp)
    assert {"dem", "tin"} <= {s["product"] for s in stamps.stale_products(ch)}
    # the DEM from a TIN made from the DEM it replaced: neither can be rebuilt
    ch.meta["dem"] = {"resolution": 0.05, "source": "tin", "classes": None}
    steps, by_hand, _kept = win._rebuild_plan(ch)
    assert not any(name in ("TIN", "DEM") for name, _fn in steps), [n for n, _f in steps]
    assert any("DEM" in n for n in by_hand) and "TIN" in by_hand
    # the DEM's own rebuild fails: the TIN on the DEM is not rebuilt on the old DEM
    ch.meta["dem"] = {"resolution": 0.05, "source": "point_cloud", "classes": None}
    built = []

    def broken(*a, **k):
        raise RuntimeError("prova")

    monkeypatch.setattr(ch, "build_dem", broken)
    monkeypatch.setattr(T, "build_tin", lambda *a, **k: built.append(k))
    win.rebuild_stale(ch)
    win.runner.wait()
    qapp.processEvents()
    assert built == []
    text = next(t for kind, t in win.said if kind == "info")
    assert "Non riusciti: DEM" in text
    left = next(line for line in text.splitlines() if line.startswith("Da rifare a mano: "))
    assert "TIN" in left.split(": ", 1)[1].split(", ")
    assert stamps.is_stale(ch, "tin")


def test_tin_rebuilt_with_its_boundary_and_classes(win, qapp, monkeypatch):
    import shapely
    from orient_helpers import motion, rotation

    from pyarchinit_photogrammetry.engine import tin as T

    ch = win.chunk
    square = _square(ch)
    T.build_tin(ch, source="dem", boundary=shapely.Polygon(square))
    ch.meta["tin"]["source"] = "cloud"
    ch.meta["tin"]["classes"] = [2]
    m = motion(rotation([0, 0, 1], 30.0), (2.0, -1.0, 0.3))  # a turn about the vertical: plan points move exactly
    win.orient_chunk(ch, M=m)
    qapp.processEvents()
    seen = []
    monkeypatch.setattr(T, "build_tin", lambda chunk, **k: seen.append(k))
    for name, fn in win._rebuild_plan(ch)[0]:
        if name == "TIN":
            fn(lambda *a, **k: None)
    assert len(seen) == 1
    k = seen[0]
    assert k["source"] == "cloud" and k["classes"] == [2]
    want = np.asarray(square) @ m[:2, :2].T + m[:2, 3]
    got = np.asarray(k["boundary"].exterior.coords)[:-1, :2]
    assert np.allclose(got, want, atol=1e-6)


def test_precision_and_coverage_follow_a_stale_dem(win, monkeypatch):
    from orient_helpers import centre

    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import fieldcheck, precision

    ch = win.chunk
    x, y, z = centre(ch)
    gx, gy = np.meshgrid(np.linspace(x - 3, x + 3, 5), np.linspace(y - 3, y + 3, 5))
    xyz = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)])
    monkeypatch.setattr(precision, "tie_point_precision", lambda chunk: (xyz, np.full((len(xyz), 3), 0.002)))
    precision.dem_precision(ch)
    assert not stamps.is_stale(ch, "dem_precision")
    used: list = []
    fieldcheck.coverage(ch, used=used)
    assert used == ["dem"]  # the heights of the cells from the DEM
    fieldcheck.field_check(ch, quality=False)
    assert not stamps.is_stale(ch, "coverage")
    ch.meta["built_in"]["dem"] = np.eye(4).tolist()  # the DEM of another system
    precision.dem_precision(ch)
    assert stamps.is_stale(ch, "dem_precision")  # on the DEM's grid: stale too
    fieldcheck.field_check(ch, quality=False)
    assert stamps.is_stale(ch, "coverage")


def test_products_on_the_dem_left_by_hand_when_its_rebuild_fails(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    win._terrain_raster_job(ch, "slope", lambda *a, **k: None)
    _orient(win, qapp)
    stale = {s["product"] for s in stamps.stale_products(ch)}
    assert {"dem", "orthomosaic", "slope"} <= stale
    ch.meta.setdefault("orthomosaic", {})["surface"] = "dem"
    slope = ch.product_path("slope")
    before = slope.stat().st_mtime_ns
    built = []

    def broken(*a, **k):
        raise RuntimeError("prova")

    monkeypatch.setattr(ch, "build_dem", broken)
    monkeypatch.setattr(ch, "build_orthomosaic", lambda *a, **k: built.append("orthomosaic"))
    win.rebuild_stale(ch)
    win.runner.wait()
    qapp.processEvents()
    assert built == []
    assert slope.stat().st_mtime_ns == before
    text = next(t for kind, t in win.said if kind == "info")
    assert "Non riusciti: DEM" in text and "Rifatti" not in text
    left = next(line for line in text.splitlines() if line.startswith("Da rifare a mano: "))
    assert "Ortofoto" in left and "Pendenza" in left
    assert stamps.is_stale(ch, "orthomosaic") and stamps.is_stale(ch, "slope")


# ---------------------------------------------------------------- 1.7.3
@pytest.mark.parametrize("surface, waits", [("dem", True), ("ground", True), ("model", False)])
def test_the_orthomosaic_waits_for_the_dem_on_every_surface_but_the_model(win, qapp, monkeypatch, surface, waits):
    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    _orient(win, qapp)
    ch.meta.setdefault("orthomosaic", {})["surface"] = surface
    built = []

    def broken(*a, **k):
        raise RuntimeError("prova")

    monkeypatch.setattr(ch, "build_dem", broken)
    monkeypatch.setattr(ch, "build_orthomosaic", lambda *a, **k: built.append(k) or stamps.stamp(ch, "orthomosaic"))
    win.settings.setValue("rebuild_keep_versions", "false")
    win.rebuild_stale(ch, [s for s in stamps.stale_products(ch) if s["product"] in ("dem", "orthomosaic")])
    win.runner.wait()
    qapp.processEvents()
    assert (built == []) is waits, built


def test_tin_rebuilt_with_its_soft_and_boundary_lines(win, qapp, monkeypatch):
    from orient_helpers import centre

    from pyarchinit_photogrammetry.archaeo import breaklines
    from pyarchinit_photogrammetry.engine import tin as T

    ch = win.chunk
    x, y, _z = centre(ch)
    soft = breaklines.new_record("soft", [(x - 0.5, y - 0.2), (x + 0.5, y + 0.2)])
    edge = breaklines.new_record("boundary", [(x - 0.8, y - 0.8), (x + 0.8, y - 0.8), (x + 0.8, y + 0.8),
                                              (x - 0.8, y + 0.8)])
    ch.meta.setdefault("drawings", {})["lines"] = [soft, edge]
    win.lines = [dict(soft), dict(edge)]
    T.build_tin(ch, source="dem", breaklines=breaklines.for_tin([soft, edge]))
    assert ch.meta["tin"]["n_breaklines"] == 0 and ch.meta["tin"]["n_lines"] == 2
    seen = []
    monkeypatch.setattr(T, "build_tin", lambda chunk, **k: seen.append(k))
    win._rebuild_tin(ch, lambda *a, **k: None)
    assert [ln["kind"] for ln in seen[0]["breaklines"]] == ["soft", "boundary"]


def _fake_dem(ch, calls):
    import shutil

    from pyarchinit_photogrammetry.core import stamps

    def build(resolution=None, source="point_cloud", interpolate=True, classes=None, progress=None):
        calls.append("dem")
        out = ch.data_dir / "dem.tif"
        kept = [e for e in (ch.meta.get("versions") or {}).get("dem") or []]
        src = ch.data_dir / kept[-1]["files"]["dem"] if kept and not ch.dem_path else ch.dem_path
        tmp = ch.data_dir / "dem_nuovo.tif"
        shutil.copy2(src, tmp)
        with open(tmp, "ab") as f:  # another file: its bytes differ
            f.write(b"\0" * 16)
        tmp.replace(out)
        ch._set_product("dem", out)
        stamps.stamp(ch, "dem")
        return out

    return build


def _digest(path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.parametrize("keep", [True, False])
def test_rebuild_keeps_the_previous_product_and_is_one_undo_step(win, qapp, monkeypatch, keep):
    from pyarchinit_photogrammetry.core import stamps, versions

    ch = win.chunk
    _orient(win, qapp)
    old_path, old_digest = ch.dem_path, _digest(ch.dem_path)
    old_versions = versions.versions(ch, "dem")
    calls = []
    monkeypatch.setattr(ch, "build_dem", _fake_dem(ch, calls))
    assert win.a_rebuild_keep.isChecked()  # on by default, as the build dialogs keep the active version
    win.a_rebuild_keep.setChecked(not keep)
    win.a_rebuild_keep.setChecked(keep)
    assert win.settings.value("rebuild_keep_versions") == ("true" if keep else "false")
    entries = len(win.history.entries)
    win.rebuild_stale(ch, [s for s in stamps.stale_products(ch) if s["product"] == "dem"])
    win.runner.wait()
    qapp.processEvents()
    assert calls == ["dem"]
    assert not stamps.is_stale(ch, "dem")
    new_digest = _digest(ch.dem_path)
    assert new_digest != old_digest
    made = versions.versions(ch, "dem")
    if keep:
        assert len(made) == len(old_versions) + 1
        vfile = versions.version_file(ch, "dem", made[-1]["id"])
        assert _digest(vfile) == old_digest  # the DEM of before, kept as a version (made in the other system)
        assert stamps.entry_stale(ch, made[-1])
    else:
        assert made == old_versions
    assert len(win.history.entries) == entries + 1  # ONE step
    assert win.history.undo_text() == "Ricostruisci i prodotti da rifare"
    win.undo()
    qapp.processEvents()
    assert ch.dem_path == old_path and _digest(ch.dem_path) == old_digest
    assert stamps.is_stale(ch, "dem")
    assert versions.versions(ch, "dem") == old_versions
    if keep:
        assert not (ch.data_dir / "versions" / "dem" / made[-1]["id"]).exists()
    win.redo()
    qapp.processEvents()
    assert not stamps.is_stale(ch, "dem") and _digest(ch.dem_path) == new_digest
    assert [e["id"] for e in versions.versions(ch, "dem")] == [e["id"] for e in made]
    if keep:
        assert _digest(versions.version_file(ch, "dem", made[-1]["id"])) == old_digest


def test_undo_of_a_rebuild_refused_after_another_change(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.core import stamps

    ch = win.chunk
    _orient(win, qapp)
    monkeypatch.setattr(ch, "build_dem", _fake_dem(ch, []))
    win.rebuild_stale(ch, [s for s in stamps.stale_products(ch) if s["product"] == "dem"])
    win.runner.wait()
    qapp.processEvents()
    ch.build_dem()  # built again by hand meanwhile (the window's DEM step is not in Modifica > Annulla)
    digest = _digest(ch.dem_path)
    win.undo()
    qapp.processEvents()
    assert _digest(ch.dem_path) == digest and not stamps.is_stale(ch, "dem")  # nothing put back
    assert win.history.undo_text() != "Ricostruisci i prodotti da rifare"


def test_elevation_kept_when_the_swap_of_its_folder_fails(win, qapp, monkeypatch):
    from orient_helpers import _write_elevation

    from pyarchinit_photogrammetry.engine import elevation
    from pyarchinit_photogrammetry.io import files

    ch = win.chunk
    old = next(e for e in ch.meta["elevations"] if e["id"] == "el000001")
    before = (ch.data_dir / old["orthophoto"]).read_bytes()

    def fake(chunk, frame, resolution=None, name=None, depth=None, bounds=None, kind="elevation", **kw):
        entry = _write_elevation(chunk, frame, "nuovo001", kind, bounds, depth, resolution)
        (chunk.data_dir / entry["orthophoto"]).write_bytes(b"nuovo")
        return entry

    monkeypatch.setattr(elevation, "build_elevation", fake)
    real = files._rename_folder
    calls = []

    def rename(src, dst):
        calls.append(1)
        if len(calls) == 2:  # the new folder into place
            raise OSError("disco pieno")
        return real(src, dst)

    monkeypatch.setattr(files, "_rename_folder", rename)
    with pytest.raises(OSError, match="disco pieno"):
        win._rebuild_elevation(ch, "el000001", lambda *a, **k: None)
    entries = [e for e in ch.meta["elevations"] if e["id"] in ("el000001", "nuovo001")]
    assert [e["id"] for e in entries] == ["el000001"] and entries[0] is old
    assert (ch.data_dir / old["orthophoto"]).read_bytes() == before
    assert not (ch.data_dir / "elevations" / "nuovo001").exists()
    assert not files.backup_name(ch.data_dir / "elevations" / "el000001").exists()


def test_wall_scale_elevation_rebuilt_on_its_own_u_map(win, monkeypatch):
    from orient_helpers import centre, cylinder_elevation

    from pyarchinit_photogrammetry.engine import elevation

    ch = win.chunk
    entry = cylinder_elevation(ch, centre(ch), eid="cy000099")
    seen = []

    class Stop(Exception):
        pass

    def fake(chunk, frame, **kw):
        seen.append(kw)
        raise Stop

    monkeypatch.setattr(elevation, "build_elevation", fake)
    with pytest.raises(Stop):
        win._rebuild_elevation(ch, "cy000099", lambda *a, **k: None)
    kw = seen[0]
    assert np.array_equal(np.asarray(kw["u_map"]), np.asarray(entry["frame"]["u_map"]))
    assert kw.get("scale", "wall") == "wall" and list(kw["bounds"]) == list(entry["bounds"])


def test_relief_steps_named_as_the_relief_menu(win):
    from pyarchinit_photogrammetry.gui.panels import product_name

    ch = win.chunk
    ch.meta["relief"] = {"slrm": {"built_in": np.diag([2.0, 2.0, 2.0, 1.0]).tolist()},
                         "svf": {"built_in": np.diag([2.0, 2.0, 2.0, 1.0]).tolist()}}
    names = [name for name, _fn in win._rebuild_plan(ch)[0]]
    assert product_name("slrm") in names and product_name("svf") in names
    assert "slrm" not in names and "svf" not in names


def test_a_raster_shown_by_a_plugin_judges_the_banner_again(win, qapp, tmp_path):
    from pyarchinit_photogrammetry.plugins.api import Context

    view = win.ortho_view
    ch = win.chunk
    _orient(win, qapp)
    win._show_raster("dem")
    assert view.banner_text().startswith(BANNER)
    context = Context("prova", tmp_path / "dati", win)
    context.show_frame((0.0, 0.0, 10.0, 10.0), 0.05)
    qapp.processEvents()
    assert view.banner_text() == ""  # an empty frame: nothing stale under it
    view.load(ch.dem_path, "dem")  # a plugin loading the stale DEM itself
    assert view.banner_text().startswith(BANNER)
    entry = next(e for e in ch.meta["elevations"] if e["id"] == "el000001")
    win.show_elevation(entry)  # rigidly moved: current
    assert view.banner_text() == ""
    view.load(ch.dem_path, "dem")  # over the elevation: the DEM is what is judged now
    assert view.banner_text().startswith(BANNER)
    assert getattr(win, "_raster_version", None) is None
