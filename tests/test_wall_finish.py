"""The presentation finishing of an elevation (:mod:`pyarchinit_photogrammetry.engine.elevation_finish`): an export,
never written into the measured elevation - the holes inside the wall's span filled (push-pull colour plus the
texture mirrored through the nearest valid cell), what lies outside the span masked, the low frequencies of the
colour levelled in Lab, the survey targets painted over - and the backlit photos left out of a build
(:func:`pyarchinit_photogrammetry.engine.elevation.blown_share`); ``ppg elevation --finish`` and
``--skip-backlit``."""
from __future__ import annotations

import json
import shutil

import cv2
import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import elevation_finish as F

from wall_helpers import model_doc

RES = 0.01
HOLE = (slice(100, 120), slice(200, 220))  # 20 x 20 cells (0.2 m): the surface measured, no photo painted it
WINDOW = (slice(80, 170), slice(440, 590))  # 1.5 m x 0.9 m: an opening through the wall, with wall above and below
GAP = (slice(150, 160), slice(50, 60))  # 0.1 m: no surface at all in the depth band - a hole through the wall
SPECK = (slice(5, 10), slice(500, 505))  # 25 cells of surface floating above the wall: a speck
TARGET_UV = (4.0, 101.0)  # the marker on the face: u along the first leg, v the height


def _top(w: int) -> np.ndarray:
    """The first row of the wall in each column: from row 30 at the left to row 60 at the right."""
    return np.round(30 + 30 * np.arange(w) / (w - 1)).astype(int)


def _cell(t, u: float, v: float) -> tuple[int, int]:
    col, row = ~t @ (u, v)
    return int(np.floor(row)), int(np.floor(col))


def _centre(t, row: int, col: int) -> tuple[float, float]:
    return t.c + (col + 0.5) * t.a, t.f + (row + 0.5) * t.e


@pytest.fixture(scope="module")
def strip(tmp_path_factory):
    """``(chunk, entry, info)``: a strip made from arrays on the grid of a plane elevation of the first leg of the
    wall at 1 cm (6 m x 2 m): a wall whose top slopes from row 30 to row 60, nothing above it but a speck, grey 150
    with noise of deviation 6, a hole of 20 x 20 cells and a black square of 10 cm where a marker of the chunk lies
    on the face. ``info``: ``top``, ``rgba`` (the source) and the marker's cell ``target``."""
    from pyarchinit_photogrammetry.testing.synthetic import make_wall_scene

    doc, ch, info = model_doc(tmp_path_factory.mktemp("finitura") / "doc", make_wall_scene())
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    plane = E.build_elevation(ch, E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, -3.0]), resolution=RES,
                              depth=(-0.5, 0.5), max_angle=89.0, bounds=(0.0, 100.0, 6.0, 102.0))
    _origin, (h, w) = E.grid_of(plane)
    rng = np.random.default_rng(5)
    rgba = np.zeros((4, h, w), np.uint8)
    rgba[:3] = np.clip(np.round(150 + rng.normal(0.0, 6.0, (3, h, w))), 0, 255).astype(np.uint8)
    rgba[3] = 255
    top = _top(w)
    rows = np.arange(h)[:, None]
    above = rows < top[None, :]
    rgba[:, above] = 0
    rgba[:3][(slice(None), *SPECK)] = 200
    rgba[3][SPECK] = 255
    relief = np.zeros((h, w), np.float32)
    relief[rgba[3] == 0] = np.nan
    rgba[:, HOLE[0], HOLE[1]] = 0                                     # the relief stays: the surface is there
    for cut in (WINDOW, GAP):
        rgba[:, cut[0], cut[1]] = 0
        relief[cut] = np.nan
    world = np.array([a[0] + TARGET_UV[0], a[1], TARGET_UV[1]])
    ch.add_marker("T1", ch.to_internal(world[None])[0])
    entry = E.entry_from_rasters(ch, plane, rgba, relief, name="Striscia", kind="elevation")
    (label, u, v, _d), = E.markers_on(ch, entry)
    assert label == "T1" and abs(u - TARGET_UV[0]) < 1e-6 and abs(v - TARGET_UV[1]) < 1e-6
    with rasterio.open(E.elevation_paths(ch, entry)[0]) as src:
        t = src.transform
    r0, c0 = _cell(t, u, v)
    for r in range(r0 - 8, r0 + 9):
        for c in range(c0 - 8, c0 + 9):
            cu, cv = _centre(t, r, c)
            if abs(cu - u) <= 0.05 and abs(cv - v) <= 0.05:
                rgba[:3, r, c] = 10
    entry = E.entry_from_rasters(ch, plane, rgba, relief, name="Striscia", kind="elevation", replace=entry)
    doc.save()
    return ch, entry, {"top": top, "rgba": rgba, "target": (u, v), "transform": t}


def _tif(path):
    with rasterio.open(path) as src:
        return src.read(), src.transform, src.tags()


def _ring(mask: np.ndarray, width: int = 10) -> np.ndarray:
    from scipy.ndimage import binary_dilation

    return binary_dilation(mask, iterations=width) & ~mask


def test_finishing_is_an_export_and_leaves_the_elevation_as_measured(strip, tmp_path):
    ch, entry, _info = strip
    ortho, relief = E.elevation_paths(ch, entry)
    before = (ortho.read_bytes(), relief.read_bytes(), json.dumps(ch.meta, sort_keys=True, default=str))
    r = F.finish(ch, entry, tmp_path / "out.tif")
    F.finish(ch, entry, tmp_path / "out.jpg")
    assert (ortho.read_bytes(), relief.read_bytes(), json.dumps(ch.meta, sort_keys=True, default=str)) == before
    assert r["path"] == str(tmp_path / "out.tif")
    for inside in (ch.data_dir / "pubblicazione.jpg", ortho.parent / "finito.tif"):
        with pytest.raises(ValueError):
            F.finish(ch, entry, inside)
        assert not inside.exists()
    with pytest.raises(ValueError):
        F.finish(ch, entry, tmp_path / "out.png")
    assert (ortho.read_bytes(), relief.read_bytes(), json.dumps(ch.meta, sort_keys=True, default=str)) == before


def test_holes_filled_sky_masked(strip, tmp_path):
    ch, entry, info = strip
    r = F.finish(ch, entry, tmp_path / "f.tif", level=False)
    out, t, tags = _tif(tmp_path / "f.tif")
    src_t = info["transform"]
    assert out.shape[0] == 4 and t == src_t and tags.get("ppg_frame") == json.dumps(entry["frame"])
    hole = np.zeros(out.shape[1:], bool)
    hole[HOLE] = True
    grey = out[:3].astype(float).mean(axis=0)
    assert (out[3][hole] == 255).all()
    around = _ring(hole)
    assert abs(grey[hole].mean() - grey[around].mean()) < 8
    assert out[0][hole].astype(float).std() >= 3                      # texture, not a flat patch
    h, w = grey.shape
    above = np.arange(h)[:, None] < info["top"][None, :]
    assert (out[3][above] == 0).all()                                 # the speck too
    assert r["filled"] >= 400 and r["masked"] > 0 and r["size"] == [w, h]
    # the window without surface stays open, the small mesh hole is filled
    assert (out[3][WINDOW] == 0).all() and (out[3][GAP] == 255).all() and r["openings"] == 1
    # the JPEG: outside the span white
    F.finish(ch, entry, tmp_path / "f.jpg", level=False)
    jpg = cv2.imread(str(tmp_path / "f.jpg"))
    assert jpg.shape[:2] == (h, w)
    far = np.arange(h)[:, None] < info["top"][None, :] - 8              # JPEG ringing: one block off the wall
    assert (jpg[far] > 250).all()
    assert (jpg[WINDOW[0].start + 8:WINDOW[0].stop - 8, WINDOW[1].start + 8:WINDOW[1].stop - 8] > 250).all()
    # not filled: the hole stays transparent; not masked: nothing is
    _r = F.finish(ch, entry, tmp_path / "nofill.tif", fill=False, level=False)
    assert (_tif(tmp_path / "nofill.tif")[0][3][hole] == 0).all() and _r["filled"] == 0
    r2 = F.finish(ch, entry, tmp_path / "nomask.tif", mask=False, level=False)
    kept = _tif(tmp_path / "nomask.tif")[0]
    assert r2["masked"] == 0 and (kept[3][SPECK] == 255).all() and (kept[:3][(slice(None), *SPECK)] == 200).all()


def test_targets_painted_over_within_18_cm(strip, tmp_path):
    ch, entry, info = strip
    src = info["rgba"]
    t = info["transform"]
    u, v = info["target"]
    r0, c0 = _cell(t, u, v)
    near = [(r, c) for r in range(r0 - 6, r0 + 7) for c in range(c0 - 6, c0 + 7)
            if np.hypot(*np.subtract(_centre(t, r, c), (u, v))) <= 0.05]
    assert min(src[:3, r, c].min() for r, c in near) < 20
    r = F.finish(ch, entry, tmp_path / "t.tif", level=False)
    out = _tif(tmp_path / "t.tif")[0]
    assert r["targets"] == 1
    assert min(out[:3, rr, cc].min() for rr, cc in near) > 100
    in_square = min(range(c0, c0 + 25), key=lambda c: abs(_centre(t, r0, c)[0] - u - 0.17))
    outside = min(range(c0, c0 + 25), key=lambda c: abs(_centre(t, r0, c)[0] - u - 0.20))
    assert not np.array_equal(out[:3, r0, in_square], src[:3, r0, in_square])
    assert np.array_equal(out[:, r0, outside], src[:, r0, outside])
    # left as they are
    r = F.finish(ch, entry, tmp_path / "keep.tif", level=False, targets=False)
    out = _tif(tmp_path / "keep.tif")[0]
    assert r["targets"] == 0 and min(out[:3, rr, cc].min() for rr, cc in near) < 20


def _noise(h, w, seed=3):
    rng = np.random.default_rng(seed)
    grey = 140 + rng.normal(0.0, 6.0, (h, w))
    return grey


def _rgb(grey):
    g = np.clip(np.round(grey), 0, 255).astype(np.uint8)
    return np.repeat(g[:, :, None], 3, axis=2)


def test_low_frequencies_levelled_in_lab(strip, tmp_path):
    assert F.LEVEL_SIGMA_PX == 250.0
    h, w = 100, 3000
    grey = _noise(h, w)
    grey[:, : w // 2] -= 30
    rgb = _rgb(grey)
    valid = np.ones((h, w), bool)
    out = F.level_colours(rgb, valid, F.LEVEL_SIGMA_PX)
    assert out.shape == rgb.shape and out.dtype == np.uint8
    g_in, g_out = rgb.astype(float).mean(axis=2), out.astype(float).mean(axis=2)
    left, right = slice(0, w // 3), slice(2 * w // 3, w)
    assert abs((g_in[:, right].mean() - g_in[:, left].mean()) - 30) < 1
    assert abs(g_out[:, right].mean() - g_out[:, left].mean()) < 5
    for c0 in (100, 1400, 2500):
        a, b = g_in[30:70, c0:c0 + 40].std(), g_out[30:70, c0:c0 + 40].std()
        assert abs(b - a) / a < 0.15                                  # the detail kept
    assert abs(g_out.mean() - g_in.mean()) < 2
    # the cells that are not valid do not pull those beside them
    grey = _noise(h, w, seed=4)
    band = slice(1400, 1600)
    grey[:, band] = 0
    valid = np.ones((h, w), bool)
    valid[:, band] = False
    out = F.level_colours(_rgb(grey), valid, F.LEVEL_SIGMA_PX).astype(float).mean(axis=2)
    assert abs(out[:, 1300:1400].mean() - out[:, 1600:1700].mean()) < 2
    assert abs(out[:, 1300:1400].mean() - 140) < 2
    # in the finishing: said, and off leaves the cells as they were
    ch, entry, info = strip
    r = F.finish(ch, entry, tmp_path / "l.tif", targets=False)
    assert r["levelled"] is True
    r = F.finish(ch, entry, tmp_path / "n.tif", level=False, targets=False)
    assert r["levelled"] is False
    out = _tif(tmp_path / "n.tif")[0]
    src = info["rgba"]
    keep = src[3] == 255
    keep[SPECK] = False                                               # outside the span: masked
    assert np.array_equal(out[:, keep], src[:, keep])


def test_too_large_is_refused_with_the_way_out(strip, tmp_path, monkeypatch):
    ch, entry, _info = strip
    w, h = entry["size"]
    with monkeypatch.context() as m:
        m.setattr(F, "MAX_PIXELS", 1000)
        with pytest.raises(ValueError, match="reduce"):
            F.finish(ch, entry, tmp_path / "big.jpg")
    assert not (tmp_path / "big.jpg").exists()
    r = F.finish(ch, entry, tmp_path / "half.tif", reduce=2)
    assert r["size"] == [(w + 1) // 2, (h + 1) // 2]
    out, t, _tags = _tif(tmp_path / "half.tif")
    assert out.shape[1:] == ((h + 1) // 2, (w + 1) // 2) and abs(t.a - 2 * RES) < 1e-12
    with pytest.raises(ValueError):
        F.finish(ch, entry, tmp_path / "zero.tif", reduce=0)


def test_span_drops_specks_and_fills_between():
    valid = np.zeros((50, 20), bool)
    valid[20:50, :] = True
    valid[30:35, 5:8] = False                                         # a hole: inside the span
    valid[2:5, 10:13] = True                                          # a speck of 9 cells
    s = F.span(valid)
    assert s[20:50].all() and not s[:20].any()
    assert F.SPECK_CELLS == 64 and F.TARGET_HALF_SIDE == 0.18 and F.MAX_PIXELS == 400_000_000


# ----------------------------------------------------------------- backlit photos


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


ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


def _backlit_photos(monkeypatch, ch, shares: dict):
    """Every photo read by the engines a plain grey; those of ``shares`` (key -> share) with that share of pixels
    blown (all three channels 255)."""
    from pyarchinit_photogrammetry.engine import ortho, visibility

    sizes = {c.key: (ch.sensor(c).height, ch.sensor(c).width) for c in ch.cameras}

    def photo(cam, flags=None):
        h, w = sizes[cam.key]
        img = np.full((h, w, 3), 120, np.uint8)
        share = shares.get(cam.key, 0.0)
        n = int(round(share * h * w))
        assert n == share * h * w or share == 0.0
        img.reshape(-1, 3)[:n] = 255
        return img

    monkeypatch.setattr(ortho, "read_photo", photo)
    monkeypatch.setattr(visibility, "read_photo", photo)


def _trench_frame(ch):
    a, b = ORIGIN[:2] + [1.05, 0.9], ORIGIN[:2] + [2.95, 0.9]
    return E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [2.0, 1.5])


def test_backlit_photos_are_left_out_and_counted(processed, tmp_path, monkeypatch):
    assert E.BACKLIT_SHARE == 0.02 and E.BLOWN_LEVEL == 250
    _doc, ch = _copy(processed, tmp_path)
    cams = sorted(ch.aligned_cameras, key=lambda c: c.label)
    h, w = ch.sensor(cams[0]).height, ch.sensor(cams[0]).width
    assert (h * w) % 50 == 0
    shares = {cams[0].key: 0.05, cams[3].key: 0.05, cams[6].key: 0.05, cams[9].key: 0.02}
    _backlit_photos(monkeypatch, ch, shares)
    assert E.blown_share(cams[0]) == pytest.approx(0.05) and E.blown_share(cams[9]) == pytest.approx(0.02)
    assert E.blown_share(cams[1]) == 0.0
    kw = dict(resolution=0.02, depth=(-0.08, 0.08), max_angle=89.0)
    entry = E.build_elevation(ch, _trench_frame(ch), skip_backlit=True, **kw)
    assert entry["backlit_skipped"] == 3 and entry["skip_backlit"] is True        # 2 % is not over 2 %
    again = E.build_like(ch, entry)
    assert again["skip_backlit"] is True and again["backlit_skipped"] == 3
    plain = E.build_elevation(ch, _trench_frame(ch), **kw)
    assert "backlit_skipped" not in plain and "skip_backlit" not in plain
    # the colour correction is measured without them too, for this build only (the chunk's own stays as it was)
    from pyarchinit_photogrammetry.engine import colour

    seen = []
    real = colour.observations

    def observations(chunk, cams=None, **kw):
        seen.append(None if cams is None else {c.key for c in cams})
        return real(chunk, cams=cams, **kw)

    monkeypatch.setattr(colour, "observations", observations)
    stored = json.dumps(ch.meta.get("colour"), sort_keys=True)
    E.build_elevation(ch, _trench_frame(ch), skip_backlit=True, colour_correction=True, **kw)
    assert seen and seen[-1] is not None and not seen[-1] & {cams[0].key, cams[3].key, cams[6].key}
    assert cams[9].key in seen[-1]
    assert json.dumps(ch.meta.get("colour"), sort_keys=True) == stored
    _backlit_photos(monkeypatch, ch, {c.key: 0.05 for c in ch.cameras})
    _touch(ch.cameras)  # the shares are kept per file: the photos changed
    with pytest.raises(RuntimeError):
        E.build_elevation(ch, _trench_frame(ch), skip_backlit=True, **kw)


def _touch(cams, seconds: float = 10.0) -> None:
    """The time of the photos of ``cams`` moved on by ``seconds`` (a photo edited: its kept share is stale)."""
    import os

    for cam in cams:
        st = os.stat(cam.path)
        os.utime(cam.path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1e9)))


def _counting(monkeypatch, ch, shares: dict) -> dict:
    """:func:`_backlit_photos`, and what the build does with each photo counted: ``decoded`` (read reduced, the
    backlit test), ``read`` (read whole, the painting), ``opened`` (its header read): camera labels in order."""
    import PIL.Image

    from pyarchinit_photogrammetry.engine import ortho, visibility

    _backlit_photos(monkeypatch, ch, shares)
    fake = visibility.read_photo
    labels = {c.path: c.label for c in ch.cameras}
    seen = {"decoded": [], "read": [], "opened": []}

    def photo(cam, flags=cv2.IMREAD_COLOR):
        seen["decoded" if flags == cv2.IMREAD_REDUCED_COLOR_4 else "read"].append(cam.label)
        return fake(cam, flags)

    real_open = PIL.Image.open

    def opened(fp, *a, **kw):
        if str(fp) in labels:
            seen["opened"].append(labels[str(fp)])
        return real_open(fp, *a, **kw)

    monkeypatch.setattr(ortho, "read_photo", photo)
    monkeypatch.setattr(visibility, "read_photo", photo)
    monkeypatch.setattr(PIL.Image, "open", opened)
    return seen


def test_a_build_reads_only_the_photos_that_see_its_part(processed, tmp_path, monkeypatch):
    """The photos of a part seen by few of them: only those are opened (once each) and looked at for the light;
    their shares are kept in the chunk, so a second build decodes none, and a photo that changed is decoded again;
    the count of the backlit ones left out stays the same."""
    import threading

    from pyarchinit_photogrammetry.engine.visibility import raycaster_for_chunk

    _doc, ch = _copy(processed, tmp_path)
    frame = _trench_frame(ch)
    kw = dict(resolution=0.02, depth=(-0.08, 0.08), max_angle=89.0)
    full = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, 0.02, threading.Lock(), kw["depth"], None)
    u0, v0 = full.bounds[:2]
    bounds = (u0, v0, u0 + 0.2, v0 + 0.2)
    part = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, 0.02, threading.Lock(), kw["depth"], bounds)
    seeing = sorted(c.label for c in E.photos_seeing(ch, part))
    others = sorted({c.label for c in ch.aligned_cameras} - set(seeing))
    assert 0 < len(seeing) < len(ch.aligned_cameras) / 2 and others
    by_label = {c.label: c for c in ch.cameras}
    shares = {by_label[seeing[0]].key: 0.05, by_label[others[0]].key: 0.05}  # one backlit in it, one out of it
    seen = _counting(monkeypatch, ch, shares)
    entry = E.build_elevation(ch, frame, bounds=bounds, skip_backlit=True, **kw)
    assert sorted(seen["decoded"]) == seeing                      # each photo that sees it decoded once, no other
    assert sorted(seen["opened"]) == seeing                       # each header read once
    assert set(seen["read"]) <= set(seeing)
    assert entry["backlit_skipped"] == 1
    stored = ch.meta["blown_shares"]
    assert sorted(by_label_of(ch, stored)) == seeing and stored[str(by_label[seeing[0]].key)][2] == pytest.approx(0.05)
    for k in seen:
        seen[k].clear()
    again = E.build_like(ch, entry)                               # the shares kept: nothing decoded
    assert seen["decoded"] == [] and again["backlit_skipped"] == 1
    assert sorted(seen["opened"]) == seeing
    seen["decoded"].clear()
    _touch([by_label[seeing[1]]])                                 # a photo changed: that one decoded again
    third = E.build_elevation(ch, frame, bounds=bounds, skip_backlit=True, **kw)
    assert seen["decoded"] == [seeing[1]] and third["backlit_skipped"] == 1


def by_label_of(ch, stored: dict) -> list[str]:
    labels = {str(c.key): c.label for c in ch.cameras}
    return [labels[k] for k in stored]


def test_the_kept_shares_carry_no_path_follow_a_moved_project_and_drop_removed_photos(processed, tmp_path,
                                                                                        monkeypatch):
    """``blown_shares`` is keyed on the cameras, not on the photos' paths: the saved project holds no path, the
    project moved with its photos (same times) decodes none again, an entry of an older project keyed on a path is
    a miss (no error) and is dropped with the entries of cameras removed from the chunk."""
    import threading

    from pyarchinit_photogrammetry.engine.visibility import raycaster_for_chunk

    doc, ch = _copy(processed, tmp_path)
    frame = _trench_frame(ch)
    kw = dict(resolution=0.02, depth=(-0.08, 0.08), max_angle=89.0)
    full = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, 0.02, threading.Lock(), kw["depth"], None)
    u0, v0 = full.bounds[:2]
    bounds = (u0, v0, u0 + 0.2, v0 + 0.2)
    part = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, 0.02, threading.Lock(), kw["depth"], bounds)
    seeing = sorted(c.label for c in E.photos_seeing(ch, part))
    assert len(seeing) >= 2
    gone = next(c for c in ch.cameras if c.label not in seeing)
    old = str(ch.data_dir.resolve() / "photos" / "vecchia.jpg")
    ch.meta["blown_shares"] = {old: [1, 2, 0.5], str(gone.key): [1, 2, 0.5]}
    seen = _counting(monkeypatch, ch, {})
    E.build_elevation(ch, frame, bounds=bounds, skip_backlit=True, **kw)
    assert sorted(seen["decoded"]) == seeing
    stored = ch.meta["blown_shares"]
    assert old not in stored and all(k.isdigit() for k in stored)
    ch.remove_cameras([gone])
    seen["decoded"].clear()
    E.build_elevation(ch, frame, bounds=bounds, skip_backlit=True, **kw)
    assert seen["decoded"] == []                                   # the kept shares: nothing decoded again
    assert sorted(by_label_of(ch, ch.meta["blown_shares"])) == seeing   # the removed camera's entry dropped
    doc.save()
    found = []

    def look(node):
        if isinstance(node, dict):
            found.extend([node["blown_shares"]] if "blown_shares" in node else [])
            for v in node.values():
                look(v)
        elif isinstance(node, list):
            for v in node:
                look(v)

    look(json.loads(doc.path.read_text()))
    assert found and all(k.isdigit() for f in found for k in f)    # no path saved, absolute or not
    moved, mch = _copy((doc, ch, None), tmp_path, name="spostato")      # copied elsewhere, same times
    seen = _counting(monkeypatch, mch, {})
    E.build_elevation(mch, frame, bounds=bounds, skip_backlit=True, **kw)
    assert seen["decoded"] == []


def test_a_part_no_photo_sees_is_refused(processed, tmp_path):
    """A part of the frame that no aligned photo sees is refused with a clear error, not built as an empty
    orthophoto; nothing is added to the chunk or written in its folder."""
    _doc, ch = _copy(processed, tmp_path)
    frame = _trench_frame(ch)
    before = len(ch.meta.get("elevations", []))
    files = sorted(p for p in ch.data_dir.rglob("*") if p.is_file())
    with pytest.raises(E.NoPhotoSees, match="no photo sees this part of the frame"):
        E.build_elevation(ch, frame, resolution=0.02, depth=(-0.08, 0.08), max_angle=89.0,
                          bounds=(500.0, 0.0, 501.0, 1.0))           # half a kilometre along the wall
    assert len(ch.meta.get("elevations", [])) == before
    assert sorted(p for p in ch.data_dir.rglob("*") if p.is_file()) == files


def test_the_photos_that_see_a_part_paint_it_as_all_the_photos(processed, tmp_path, monkeypatch):
    """Building from the photos that see the part only (:func:`elevation.photos_seeing`) paints the same elevation
    as from every photo of the chunk."""
    _doc, ch = _copy(processed, tmp_path)
    kw = dict(resolution=0.02, depth=(-0.08, 0.08), max_angle=89.0)
    real = E.photos_seeing
    counts = []

    def seeing(chunk, surf, cams=None):
        out = real(chunk, surf, cams)
        counts.append(len(out))
        return out

    monkeypatch.setattr(E, "photos_seeing", seeing)
    narrow = E.build_elevation(ch, _trench_frame(ch), **kw)
    assert counts[-1] < len(ch.aligned_cameras)
    monkeypatch.setattr(E, "photos_seeing", lambda chunk, surf, cams=None: list(chunk.aligned_cameras))
    wide = E.build_elevation(ch, _trench_frame(ch), **kw)
    with rasterio.open(ch.data_dir / narrow["orthophoto"]) as a, rasterio.open(ch.data_dir / wide["orthophoto"]) as b:
        x, y = a.read(), b.read()
    assert x.shape == y.shape and (x[3] > 0).sum() > 1000
    assert np.array_equal(x, y)


def test_command_line_finish_and_skip_backlit(strip, processed, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    ch, entry, _info = strip
    project = str(ch.document.path)
    run = CliRunner()

    def go(*args, code=0):
        r = run.invoke(app, [str(a) for a in args])
        assert r.exit_code == code, r.output + repr(r.exception)
        return r.output

    out = tmp_path / "muro.jpg"
    text = go("elevation", project, "--like", entry["id"], "--finish", out, "--no-level", "--reduce", 2)
    assert out.exists() and str(out) in text and "filled" in text and "1 opening left open" in text
    text = go("elevation", project, "--like", entry["id"], "--finish", tmp_path / "wide.tif", "--max-hole", 2.0)
    assert "0 openings left open" in text                             # max 2 m: the window is filled too
    assert "--finish" in go("elevation", project, "--like", entry["id"], "--max-hole", 1.0, code=2)
    assert "real shading" in " ".join(go("elevation", "--help").replace("│", " ").split())
    w, h = entry["size"]
    assert cv2.imread(str(out)).shape[:2] == ((h + 1) // 2, (w + 1) // 2)
    go("elevation", project, "--like", entry["id"], "--finish", tmp_path / "muro.tif", "--no-fill", "--no-mask",
       "--no-targets")
    assert (tmp_path / "muro.tif").exists()
    assert "--finish" in go("elevation", project, "--like", entry["id"], "--no-fill", code=2)
    go("elevation", project, "--like", entry["id"], "--finish", ch.data_dir / "x.jpg", code=1)
    # a build that leaves the backlit photos out
    doc, tch = _copy(processed, tmp_path, "cli")
    cams = sorted(tch.aligned_cameras, key=lambda c: c.label)
    _backlit_photos(monkeypatch, tch, {cams[0].key: 0.05, cams[1].key: 0.05})
    a, b = ORIGIN[:2] + [1.05, 0.9], ORIGIN[:2] + [2.95, 0.9]
    text = go("elevation", doc.path, "--trace", f"{a[0]},{a[1]},{b[0]},{b[1]}", "--side",
              f"{ORIGIN[0] + 2.0},{ORIGIN[1] + 1.5}", "--resolution", 0.02, "--front", 0.08, "--back", 0.08,
              "--skip-backlit")
    assert "2 backlit photos left out" in text
    import pyarchinit_photogrammetry as ppg

    made = E.elevations(ppg.Document.open(doc.path).chunk)[-1]
    assert made["skip_backlit"] is True and made["backlit_skipped"] == 2


def test_openings_are_never_filled(strip, tmp_path):
    """A hole up to ``max_hole`` metres across is always filled - the 0.2 m hole whose surface was measured and the
    0.1 m hole in the mesh without relief; a larger one only when the relief finds surface in it (a gap in the
    texture on the geometry): the 1.5 m window without surface stays open, with its surface measured it is filled; a
    smaller max_hole makes the small mesh hole an opening too."""
    ch, entry, info = strip
    assert F.MAX_HOLE == 0.5
    r = F.finish(ch, entry, tmp_path / "a.tif", level=False, targets=False)
    out = _tif(tmp_path / "a.tif")[0]
    assert (out[3][HOLE] == 255).all() and (out[3][GAP] == 255).all() and (out[3][WINDOW] == 0).all()
    assert r["openings"] == 1
    r = F.finish(ch, entry, tmp_path / "small.tif", level=False, targets=False, max_hole=0.05)
    out = _tif(tmp_path / "small.tif")[0]
    assert (out[3][HOLE] == 255).all() and (out[3][GAP] == 0).all() and r["openings"] == 2
    # the window with its surface measured (the photos missed it): a gap in the texture, filled
    rgba, relief = info["rgba"].copy(), _tif(ch.data_dir / entry["relief"])[0][0]
    relief[WINDOW] = 0.0
    seen = E.entry_from_rasters(ch, entry, rgba, relief, name="Finestra vista", kind="elevation")
    try:
        r = F.finish(ch, seen, tmp_path / "b.tif", level=False, targets=False)
        assert (_tif(tmp_path / "b.tif")[0][3][WINDOW] == 255).all() and r["openings"] == 0
    finally:
        E.remove_elevation(ch, seen["id"])
