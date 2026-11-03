"""Patches on elevations and sections: inside a polygon drawn on the elevation (u, v metres) the colours come from
the photos chosen by hand, stored on the elevation's own entry and redrawn in its file only. Every photo is replaced
by a plain colour of its own (as in tests/test_patches.py), so the colour of a pixel tells which photo it comes from."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import patches as P

from elevation_helpers import ORIGIN
from elevation_helpers import colour_of as _colour_of
from elevation_helpers import fake_photo as _fake_photo
from elevation_helpers import plain as _plain
from elevation_helpers import prepare as _prepare
from elevation_helpers import surface_cell as _surface_cell


@pytest.fixture
def plain_photos(monkeypatch):
    """Every photo read by the engines is a plain colour of its own."""
    _plain(monkeypatch)
    return _fake_photo


@pytest.fixture(scope="module")
def elevations_base(processed, tmp_path_factory):
    """A copy of the processed project with the south wall of the trench (as tests/test_elevation_drawings.py) and a
    well (a cylinder seen from its axis), both built from the plain photos."""
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path_factory.mktemp("patch_prospetti") / "base"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    ch = out.chunk
    _prepare(ch)
    with pytest.MonkeyPatch.context() as mp:
        _plain(mp)
        wall = E.build_elevation(ch, E.plane_from_line(ch, ORIGIN[:2] + [1.05, 0.9], ORIGIN[:2] + [2.95, 0.9],
                                                       side=ORIGIN[:2] + [2.0, 1.5]),
                                 resolution=0.01, depth=(-0.08, 0.08), max_angle=89.0, name="Parete sud")
        ring = [ORIGIN[:2] + [2 + 0.5 * np.cos(t), 1.5 + 0.5 * np.sin(t)] for t in (0.3, 2.4, 4.4)]
        well = E.build_elevation(ch, E.cylinder_from_circle(ch, ring, inside=True), resolution=0.02,
                                 depth=(-2.0, 0.4), max_angle=89.0, name="Pozzo")
    out.save()
    return out.path, wall["id"], well["id"]


def _open(base, tmp_path, which: int):
    import pyarchinit_photogrammetry as ppg

    path = base[0]
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(path, d / path.name)
    shutil.copytree(path.parent / (path.name + ".files"), d / (path.name + ".files"))
    shutil.copytree(path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / path.name)
    ch = doc.chunk
    _prepare(ch)
    eid = base[which]
    return ch, eid, P.entry_of(ch, eid)


@pytest.fixture
def wall(elevations_base, tmp_path):
    return _open(elevations_base, tmp_path, 1)


@pytest.fixture
def well(elevations_base, tmp_path):
    return _open(elevations_base, tmp_path, 2)


def _read(path):
    with rasterio.open(path) as src:
        return src.read()


def _transform(path):
    with rasterio.open(path) as src:
        return src.transform


def _cells(ch, entry, poly, shape, margin: float = 0.015):
    """Masks of the cells of the elevation (``shape``: rows, columns) inside ``poly`` shrunk by ``margin`` and with
    relief, and outside it grown by ``margin``."""
    from shapely import contains_xy

    t = _transform(E.elevation_paths(ch, entry)[0])
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    u, v = t.c + (cols + 0.5) * t.a, t.f + (rows + 0.5) * t.e
    relief = _read(E.elevation_paths(ch, entry)[1])[0]
    inside = contains_xy(poly.buffer(-margin), u, v) & np.isfinite(relief)
    outside = ~contains_xy(poly.buffer(margin), u, v)
    return inside, outside


def plain_colour(ch, label):
    """The RGB colour of a photo under ``plain_photos``."""
    return np.array(_colour_of(ch.camera(label).key)[::-1], float)


def _share(img, inside, rgb):
    return float((np.abs(img[:3, inside].T.astype(int) - rgb).max(axis=1) <= 1).mean())


def test_a_photo_assigned_on_the_wall_paints_its_cells_only(wall, plain_photos):
    ch, eid, entry = wall
    path0 = E.elevation_paths(ch, entry)[0]
    before = _read(path0)
    with rasterio.open(path0) as src:
        overviews_before = src.overviews(1)
    plan_before = ch.ortho_patches
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.1, u + 0.15, v + 0.1)
    cands = P.photos_for_area(ch, poly, frame=eid)
    assert cands and all(c["coverage"] > 0 for c in cands[:3]) and all(c["angle"] < 60 for c in cands[:3])
    # a photo the elevation hardly uses there now, so that the patch changes the cells
    pick = min(cands[:4], key=lambda c: c["used"])
    label = pick["label"]
    p = P.add_patch(ch, poly, [label], frame=eid)
    assert p["id"] == "p1" and P.patches(ch, frame=eid) == [p] and P.patches(ch) == []  # the plan untouched
    assert ch.ortho_patches == plan_before == [] and ch.elevation_patches(eid) == [p]
    assert [x["id"] for x in P.changed_patches(ch, frame=eid)] == ["p1"] and P.changed_patches(ch) == []
    relief_before = _read(E.elevation_paths(ch, entry)[1])
    path = ch.update_elevation(eid)
    assert path == path0
    after = _read(path)
    inside, outside = _cells(ch, entry, poly, after.shape[1:])
    assert inside.sum() > 200
    assert _share(after, inside, plain_colour(ch, label)) > 0.9
    assert (after[:3][:, inside] != before[:3][:, inside]).any(axis=0).mean() > 0.5
    assert (after[:, outside] == before[:, outside]).all()
    assert np.allclose(after[:3][:, inside].mean(axis=1), plain_colour(ch, label), atol=3)
    assert np.array_equal(_read(E.elevation_paths(ch, entry)[1]), relief_before, equal_nan=True)  # relief untouched
    assert P.changed_patches(ch, frame=eid) == [] and P.update_elevation(ch, eid) == path
    assert entry["applied_patches"] == [p]
    with rasterio.open(path) as src:
        assert src.overviews(1) == overviews_before  # as built (a wall 36 cells high has none)
        small = src.read(1, out_shape=(src.height // 2, src.width // 2))
    assert small.shape == (after.shape[1] // 2, after.shape[2] // 2)


def test_an_excluded_photo_is_left_out_on_the_wall(wall, plain_photos):
    ch, eid, entry = wall
    before = _read(E.elevation_paths(ch, entry)[0])
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.2, v - 0.12, u + 0.2, v + 0.12)
    most = max(P.photos_for_area(ch, poly, frame=eid), key=lambda c: c["used"])
    inside, outside = _cells(ch, entry, poly, before.shape[1:])
    colour = plain_colour(ch, most["label"])
    assert _share(before, inside, colour) > 0.05
    P.add_patch(ch, poly, [most["label"]], mode="exclude", frame=eid)
    after = _read(P.update_elevation(ch, eid))
    assert _share(after, inside, colour) < 0.01
    assert (after[:, outside] == before[:, outside]).all()


def test_clearing_the_patches_redraws_their_cells(wall, plain_photos):
    ch, eid, entry = wall
    before = _read(E.elevation_paths(ch, entry)[0])
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.1, u + 0.15, v + 0.1)
    label = min(P.photos_for_area(ch, poly, frame=eid)[:4], key=lambda c: c["used"])["label"]
    p = P.add_patch(ch, poly, [label], frame=eid)
    patched = _read(P.update_elevation(ch, eid))
    inside, _outside = _cells(ch, entry, poly, patched.shape[1:])
    assert (patched[:3][:, inside] != before[:3][:, inside]).any()
    P.clear_patches(ch, frame=eid)
    assert P.patches(ch, frame=eid) == [] and P.changed_patches(ch, frame=eid) == [p]
    assert P.patches(ch) == [] and "patches" not in (ch.meta.get("orthomosaic") or {})  # the plan's list untouched
    again = _read(P.update_elevation(ch, eid))
    assert np.abs(again.astype(int) - before).max() <= 3
    assert P.changed_patches(ch, frame=eid) == [] and entry["applied_patches"] == []


def test_the_overviews_of_the_patched_cells_are_refreshed(wall, plain_photos):
    from rasterio.enums import Resampling

    ch, eid, entry = wall
    path = E.elevation_paths(ch, entry)[0]
    with rasterio.open(path, "r+") as dst:
        dst.build_overviews([2], Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    def half():
        with rasterio.open(path) as src:
            assert src.overviews(1) == [2]
        with rasterio.open(path, overview_level=0) as ovr:  # the overview itself (no resampling of the read)
            return ovr.read()

    full_before, half_before = _read(path), half()
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.1, u + 0.15, v + 0.1)
    label = min(P.photos_for_area(ch, poly, frame=eid)[:4], key=lambda c: c["used"])["label"]
    P.add_patch(ch, poly, [label], frame=eid)
    P.update_elevation(ch, eid)
    full_after, half_after = _read(path), half()
    inside, outside = _cells(ch, entry, poly, full_after.shape[1:], margin=0.03)
    h2, w2 = full_after.shape[1] // 2, full_after.shape[2] // 2  # the overview cells over whole 2 x 2 blocks
    half_before, half_after = half_before[:, :h2, :w2], half_after[:, :h2, :w2]

    def blocks(mask):
        return mask[:2 * h2, :2 * w2].reshape(h2, 2, w2, 2).all(axis=(1, 3))

    all_in = blocks(inside)
    assert all_in.sum() > 20
    assert (half_after[:3][:, all_in] != half_before[:3][:, all_in]).any(axis=0).mean() > 0.5
    assert _share(half_after, all_in, plain_colour(ch, label)) > 0.9
    # the refreshed overview is the average of the cells as they are now, inside and outside the polygon (cells
    # with a transparent one among the four are left out: GDAL and overviews.refresh weigh them differently)
    opaque = blocks(full_after[3] == 255)
    mean = full_after[:, :2 * h2, :2 * w2].reshape(4, h2, 2, w2, 2).mean(axis=(2, 4))
    assert (opaque & blocks(outside)).sum() > 100
    assert np.abs(half_after[:, opaque] - mean[:, opaque]).max() <= 2
    assert (full_after[:, outside] == full_before[:, outside]).all()


def test_a_file_on_another_grid_is_refused(wall, plain_photos):
    from rasterio.transform import Affine

    ch, eid, entry = wall
    path = E.elevation_paths(ch, entry)[0]
    with rasterio.open(path, "r+") as dst:
        t = dst.transform
        dst.transform = Affine(t.a * 1.1, 0.0, t.c, 0.0, t.e * 1.1, t.f)  # same origin and size, other pixel
    u, v = _surface_cell(ch, entry)
    P.add_patch(ch, box(u - 0.1, v - 0.1, u + 0.1, v + 0.1), [ch.cameras[0].label], frame=eid)
    with pytest.raises(RuntimeError, match="does not match"):
        P.update_elevation(ch, eid)
    assert P.changed_patches(ch, frame=eid) != []


def test_old_entries_paint_as_they_were_built(wall, plain_photos, monkeypatch):
    """An entry of 1.7.0 has no blend_sigma, max_angle, max_cameras, ghosting, fill_holes: it is redrawn as
    build_elevation drew it (a seamless mosaic), not as an orthomosaic of before the mosaic ("disabled")."""
    from pyarchinit_photogrammetry.engine import ortho

    ch, eid, entry = wall
    for key in ("max_angle", "max_cameras", "blend_sigma", "ghosting", "fill_holes"):
        assert key in entry
        entry.pop(key)
    assert E.entry_settings(entry) == {"blending": "mosaic", "blend_sigma": ortho.BLEND_SIGMA, "max_angle": 80.0,
                                       "max_cameras": 8, "ghosting": False, "fill_holes": True,
                                       "colour_correction": False}
    seen = {}
    real = ortho._context

    def spy(*args, **kwargs):
        ctx = real(*args, **kwargs)
        seen.update(blending=ctx.blending, sigma=ctx.blend_sigma, min_cos=ctx.min_cos, max_cameras=ctx.max_cameras,
                    ghosting=ctx.ghosting, fill_holes=ctx.fill_holes)
        return ctx

    monkeypatch.setattr(ortho, "_context", spy)
    before = _read(E.elevation_paths(ch, entry)[0])
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.1, u + 0.15, v + 0.1)
    label = ch.cameras[0].label
    P.add_patch(ch, poly, [label], frame=eid)
    P.update_elevation(ch, eid)
    assert seen == {"blending": "mosaic", "sigma": ortho.BLEND_SIGMA, "min_cos": pytest.approx(np.cos(np.radians(80))),
                    "max_cameras": 8, "ghosting": False, "fill_holes": True}
    # without the patch the cells come back as build_elevation made them
    P.remove_patch(ch, "p1", frame=eid)
    again = _read(P.update_elevation(ch, eid))
    assert np.abs(again.astype(int) - before).max() <= 3


def test_a_patch_beyond_the_relief_and_the_bounds(wall, plain_photos):
    ch, eid, entry = wall
    before = _read(E.elevation_paths(ch, entry)[0])
    relief = _read(E.elevation_paths(ch, entry)[1])[0]
    u0, v0, u1, v1 = entry["bounds"]
    # over the right end of the elevation, half outside its bounds and over cells with and without relief
    poly = box(u1 - 0.3, v0 - 0.2, u1 + 0.3, v1 + 0.2)
    label = ch.cameras[0].label
    P.add_patch(ch, poly, [label], frame=eid)
    after = _read(P.update_elevation(ch, eid))
    inside, outside = _cells(ch, entry, poly, after.shape[1:], margin=0.0)
    assert (after[:, outside] == before[:, outside]).all()
    empty = ~np.isfinite(relief)
    assert empty.any() and (after[:, empty] == before[:, empty]).all()  # no relief, nothing drawn
    assert inside.any() and P.changed_patches(ch, frame=eid) == []


def test_ground_is_refused_on_an_elevation(wall):
    ch, eid, entry = wall
    u, v = _surface_cell(ch, entry)
    with pytest.raises(ValueError, match="surface 'dem'"):
        P.add_patch(ch, box(u - 0.1, v - 0.1, u + 0.1, v + 0.1), [ch.cameras[0].label], surface="ground", frame=eid)
    assert P.patches(ch, frame=eid) == []
    with pytest.raises(KeyError):
        P.entry_of(ch, "nessuno")


def test_previews_on_the_wall(wall, plain_photos):
    from shapely import contains_xy

    ch, eid, entry = wall
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.1, u + 0.15, v + 0.1)
    ring = np.asarray(poly.exterior.coords)
    ranked = P.photos_for_area(ch, poly, frame=eid)[:3]
    imgs, now, bounds = P.previews(ch, ring, [d["label"] for d in ranked], max_side=100, frame=eid)
    assert set(imgs) == {(d["label"], "dem") for d in ranked}
    _b, shape, res = P.preview_grid(ring, 100)
    assert now.shape == (*shape, 4) and np.allclose(bounds, _b)
    assert np.array_equal(now, P.current_area(ch, bounds, shape, path=E.elevation_paths(ch, entry)[0]))
    assert (now[..., 3] == 255).any()  # read from elevation.tif, not from the plan's orthomosaic
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    inside = contains_xy(poly, bounds[0] + (cols + 0.5) * res, bounds[3] - (rows + 0.5) * res)
    for d in ranked:
        img = imgs[(d["label"], "dem")]
        assert np.array_equal(img[~inside], now[~inside])
        seen = inside & (img[..., 3] == 255)
        assert seen.sum() > 20
        want = plain_colour(ch, d["label"])
        assert (np.abs(img[seen, :3].astype(int) - want).max(axis=1) <= 1).mean() > 0.99


def test_candidates_on_a_cylinder_look_along_the_view(well, plain_photos):
    """On a cylinder the angle of a photo is measured from the direction the area is seen from (towards the axis),
    not from the vertical: a wall of the well seen by the photos of the oblique ring is not 'from above'."""
    ch, eid, entry = well
    frame = E.frame_from_dict(entry["frame"])
    u, v = _surface_cell(ch, entry)
    poly = box(u - 0.15, v - 0.08, u + 0.15, v + 0.08)
    cands = P.photos_for_area(ch, poly, frame=eid, limit=50)
    assert cands and cands[0]["angle"] < 60
    # the angles are those to the view of the area: recomputed here for the best photo
    view = frame.view(np.array([u]), np.array([v]))[0]
    cam = ch.camera(cands[0]["label"])
    from pyarchinit_photogrammetry.engine.elevation import relief_at

    d = relief_at(ch, entry, u, v)
    point = frame.point(np.array([u]), np.array([v]), np.array([d]))[0]
    ray = point - ch.to_world(np.asarray(cam.center)[None])[0]
    ray /= np.linalg.norm(ray)
    assert abs(np.degrees(np.arccos(np.clip(-ray @ view, -1, 1))) - cands[0]["angle"]) < 15
