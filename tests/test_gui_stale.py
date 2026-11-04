"""Rasters built before the last change of the chunk's system: the 2D view says so in a banner, drawing on them is
refused (to plugins too), *Ricostruisci i prodotti da rifare* builds them again in order with the parameters each was
built with, and a kept version made in another system says so when it becomes the active one."""
from __future__ import annotations

import os

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
    view.set_banner("prova")
    win._release_views()  # clear_raster
    assert view.banner_text() == "prova"
    view.set_banner(None)
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
    assert new["orthophoto"] == "elevations/el000001/elevation.tif"
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
    assert "Da rifare a mano: TIN" in text
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
