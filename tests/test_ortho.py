"""The orthomosaic made tile by tile: any size (wider than 32767 px), every surface, the same
result with one or more workers; its photo cache, sized on the memory of the computer, and the
counts the orthomosaic keeps of it."""
from __future__ import annotations

import logging
import shutil
import threading
import time
import types

import numpy as np
import pytest
import rasterio
from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _env
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.engine import nodes, ortho
from pyarchinit_photogrammetry.markers.targets import detect


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


def _targets_error(path, scene) -> float:
    with rasterio.open(path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
    errors = []
    for d in detect(img):
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        errors.append(np.hypot(x - tx, y - ty))
    assert len(errors) >= 6
    return float(np.max(errors))


def test_surfaces_and_workers(processed, scene, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    one = ch.build_orthomosaic(resolution=0.004, workers=1)
    with rasterio.open(one) as src:
        a = src.read()
        assert src.overviews(1)  # pyramids for a quick display
    many = ch.build_orthomosaic(resolution=0.004, workers=4)
    with rasterio.open(many) as src:
        b = src.read()
    differ = np.any(a != b, axis=0)
    if differ.any():  # say where, if it ever happens again (it did with OpenBLAS splitting products)
        rows, cols = np.nonzero(differ)
        raise AssertionError(f"1 and 4 workers: {int(differ.sum())} pixels differ, rows {rows.min()}-{rows.max()}, "
                             f"columns {cols.min()}-{cols.max()}, up to {int(np.abs(a.astype(int) - b).max())}")
    worst = _targets_error(many, scene)
    assert worst < 0.005, f"targets in the orthomosaic off by up to {worst * 1000:.2f} mm"
    # a true orthophoto on the mesh (vertical rays), and from the dense cloud alone
    worst = _targets_error(ch.build_orthomosaic(resolution=0.004, surface="model"), scene)
    assert worst < 0.006, f"targets in the true orthophoto off by up to {worst * 1000:.2f} mm"
    ch.products.pop("dem")
    ch.products.pop("model")
    ch.products.pop("model_textured", None)
    path = ch.build_orthomosaic(resolution=0.004)
    with rasterio.open(path) as src:
        assert (src.read(4) > 0).mean() > 0.6
    assert _targets_error(path, scene) < 0.006


def test_dem_surface_wider_than_opencv_maps(tmp_path):
    """OpenCV remaps at most 32767 px a side: the whole grid of a 45000 px orthophoto stopped
    with an assertion. The DEM is now read and resampled one window at a time."""
    from rasterio.transform import from_origin

    from pyarchinit_photogrammetry.engine.ortho import _DemSurface

    w, h = 40000, 6
    z = np.tile(np.arange(w, dtype=np.float32) * 0.001, (h, 1))
    z[:, 1000:1010] = -32767.0
    path = tmp_path / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", nodata=-32767.0,
                       transform=from_origin(500000.0, 4000000.0, 0.05, 0.05), tiled=True, blockxsize=256,
                       blockysize=256) as dst:
        dst.write(z, 1)
    s = _DemSurface(path, 0.05)
    assert s.shape == (h, w)
    tile = s.tile(0, 39000, h, 1000)
    assert np.allclose(tile, z[:, 39000:40000], atol=1e-4)
    gap = s.tile(0, 990, h, 30)
    assert np.isnan(gap[:, 11:19]).all() and np.isfinite(gap[:, :9]).all()
    half = _DemSurface(path, 0.025)  # finer than the DEM: interpolated between its cells
    assert half.shape == (2 * h, 2 * w)
    t2 = half.tile(0, 70000, 2, 100)
    assert np.all(np.diff(t2[1][np.isfinite(t2[1])]) > 0)


def test_cancelled_orthomosaic_leaves_no_file(processed, tmp_path):
    import pytest

    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    doc, ch = _copy(processed, tmp_path)
    before = ch.orthomosaic_path.read_bytes()
    prog = Progress()

    def stop(fraction, _text=""):
        if fraction > 0.1:
            prog.cancel_requested = True

    prog.callback = stop
    with pytest.raises(Cancelled):
        ch.build_orthomosaic(resolution=0.002, progress=prog)
    assert not (ch.data_dir / "orthomosaic.tmp.tif").exists()
    assert ch.orthomosaic_path.read_bytes() == before  # the orthomosaic of before is still there


def test_grazing_views_leave_the_cells_transparent(processed, tmp_path):
    """Cells seen only from far from the vertical (an orbit of oblique photos, a DEM interpolated
    beyond the reconstruction) smeared the photo over the surface: they stay transparent."""
    doc, ch = _copy(processed, tmp_path)

    def coverage(path):
        with rasterio.open(path) as src:
            return float((src.read(4) > 0).mean())

    normal = coverage(ch.build_orthomosaic(resolution=0.008))
    strict = coverage(ch.build_orthomosaic(resolution=0.008, max_angle=12.0))
    assert ch.meta["orthomosaic"]["max_angle"] == 12.0
    # how much falls out depends on the margins of the DEM seen only obliquely: a few points
    # (5 with the two-view depth maps, 3.4 with PatchMatch, which leaves less ground to interpolate)
    assert normal > 0.7 and strict < normal - 0.02


def test_a_float_source_takes_the_best_photo_that_has_a_value(processed, tmp_path):
    """A float source (a thermal frame, a registered band) has no value over parts of its photo: a
    cell takes the best photo that has one there, however many better photos have none - as the
    low strips of a flight whose narrower thermal frames miss the cell that their visible photos
    frame at the edge (a hole of 12 % in the paired thermal orthomosaic of a real flight)."""
    from pyarchinit_photogrammetry.engine.ortho import build_orthomosaic
    from pyarchinit_photogrammetry.engine.visibility import read_photo

    doc, ch = _copy(processed, tmp_path)
    up = np.linalg.inv(ch.transform)[:3, :3] @ np.array([0.0, 0.0, 1.0])
    up /= np.linalg.norm(up)

    def looks_down(cam) -> bool:
        return float(-(cam.transform[:3, :3] @ np.array([0.0, 0.0, 1.0])) @ up) > np.cos(np.radians(15))

    nadir = {c.key for c in ch.aligned_cameras if looks_down(c)}
    assert 10 <= len(nadir) < len(ch.aligned_cameras) - 4  # the obliques: the photos with values

    def source(cam):
        img = read_photo(cam).astype(np.float32)[::2, ::2]  # a band on a reduced grid, like the thermal ones
        return np.full_like(img, np.nan) if cam.key in nadir else img

    def read(path):
        with rasterio.open(path) as src:
            return src.read(1)

    mixed = read(build_orthomosaic(ch, resolution=0.008, source=source, out_path=tmp_path / "mixed.tif"))
    for cam in ch.aligned_cameras:  # the same without the photos that have no value
        if cam.key in nadir:
            cam.transform = None
    alone = read(build_orthomosaic(ch, resolution=0.008, source=source, out_path=tmp_path / "alone.tif"))
    assert np.isfinite(alone).mean() > 0.5
    both = np.isfinite(mixed) & np.isfinite(alone)
    assert both.sum() >= 0.995 * np.isfinite(alone).sum()  # every cell the obliques see has its value
    assert np.median(np.abs(mixed[both] - alone[both])) < 0.5


# ---------------------------------------------------------------- the photo cache, its budget and its counts
def _cam(name):
    return types.SimpleNamespace(key=name)


def _cache(max_items, shape=(10, 10, 3), max_bytes=None):
    """A cache whose photos are zeros of ``shape`` (300 bytes for the default)."""
    return ortho._PhotoCache(max_items, loader=lambda cam: np.zeros(shape, np.uint8), max_bytes=max_bytes)


def test_the_photo_cache_holds_what_fits_and_counts_the_photos_read_again():
    cache = _cache(2, max_bytes=1500)  # 300 bytes each
    for cam in "abcde":
        cache.get(_cam(cam))
    for cam in "abcde":
        cache.get(_cam(cam))
    assert (cache.reads, cache.hits, cache.rereads) == (5, 5, 0)
    cache.get(_cam("f"))
    cache.get(_cam("a"))  # "f" pushed out "a", the least recently used
    assert cache.rereads == 1 and cache.stats()["peak_bytes"] <= 1500


def test_the_photo_cache_keeps_its_floor_and_returns_a_photo_larger_than_the_budget():
    cache = _cache(3, shape=(100, 100, 3), max_bytes=1000)
    for cam in "abc":
        assert cache.get(_cam(cam)).shape == (100, 100, 3)
    for cam in "abc":
        cache.get(_cam(cam))
    assert (cache.reads, cache.hits, cache.rereads) == (3, 3, 0)


def test_the_budget_is_35_percent_of_the_memory_with_a_floor_on_the_photos(monkeypatch):
    monkeypatch.delenv("PPG_PHOTO_CACHE_MB", raising=False)
    assert ortho.photo_cache_budget(2, 24 * 2**30) == (12, int(0.35 * 24 * 2**30))
    assert ortho.photo_cache_budget(8, None) == (24, None)
    monkeypatch.setenv("PPG_PHOTO_CACHE_MB", "512")
    assert ortho.photo_cache_budget(2, 24 * 2**30) == (12, 512 * 2**20)


@pytest.mark.parametrize("max_bytes", [None, 0])
def test_without_a_budget_the_cache_keeps_its_photos_as_version_1_10_did(max_bytes):
    """No budget (the memory unknown) and a budget of nothing (``PPG_PHOTO_CACHE_MB=0``) both keep ``max_items``
    photos and drop the least recently used: the cache of version 1.10, which the counts now describe."""
    cache = _cache(2, max_bytes=max_bytes)
    for name in "abccba":  # a, b read; c read, a dropped; c, b given; a read again (held once, then dropped)
        cache.get(_cam(name))
    assert cache.stats() == {"hits": 2, "reads": 4, "rereads": 1, "max_items": 2, "max_bytes": max_bytes,
                             "peak_bytes": 600}
    for name, held in (("b", True), ("a", True), ("c", False)):
        hits = cache.hits
        cache.get(_cam(name))
        assert (cache.hits == hits + 1) is held
    assert cache.rereads == 2


def test_a_photo_that_is_a_view_counts_the_array_behind_it():
    """A band on a reduced grid (``[::2, ::2]`` of a photo, as a thermal source gives it) keeps the whole photo in
    memory: the cache counts the bytes of the array that owns the data, not those of the view."""
    whole = np.zeros((20, 20, 3), np.float32)  # 4800 bytes; the view 1200
    cache = ortho._PhotoCache(1, loader=lambda cam: whole[::2, ::2], max_bytes=6000)
    for name in "aba":
        cache.get(_cam(name))
    assert cache.rereads == 1 and cache.stats()["peak_bytes"] == 4800


def test_the_counts_of_a_cache_in_another_process_are_added_once():
    """A node sends with each unit of tiles what its cache counted since the unit before (``new_counts``); the main
    program adds it to its own cache (``add_counts``), which refuses anything but three whole numbers - a node of a
    version without the counts sends none - and keeps its own sizes."""
    theirs = _cache(2, max_bytes=1500)
    for name in "abab":
        theirs.get(_cam(name))
    assert theirs.new_counts() == {"hits": 2, "reads": 2, "rereads": 0}
    theirs.get(_cam("a"))
    assert theirs.new_counts() == {"hits": 1, "reads": 0, "rereads": 0}
    assert theirs.new_counts() == {"hits": 0, "reads": 0, "rereads": 0}
    mine = _cache(2, max_bytes=1500)
    mine.get(_cam("z"))
    assert mine.add_counts({"hits": 2, "reads": 2, "rereads": 1})
    assert (mine.hits, mine.reads, mine.rereads) == (2, 3, 1)
    for wrong in (None, {}, [2, 2, 1], {"hits": 1, "reads": 1}, {"hits": 1, "reads": -1, "rereads": 0},
                  {"hits": 1.0, "reads": 1, "rereads": 0}, {"hits": True, "reads": 1, "rereads": 0}):
        assert not mine.add_counts(wrong)
    assert (mine.hits, mine.reads, mine.rereads) == (2, 3, 1)  # nothing of them was added
    assert mine.stats()["peak_bytes"] == 300


def test_the_counts_hold_under_several_workers():
    cache = _cache(2, max_bytes=1500)

    def worker(seed):
        for name in np.random.default_rng(seed).choice(list("abcdefgh"), 200):
            cache.get(_cam(str(name)))

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = cache.stats()
    assert stats["hits"] + stats["reads"] == 800 and 0 < stats["rereads"] <= stats["reads"]
    assert stats["peak_bytes"] <= 1500 and cache._bytes == 300 * len(cache._items) <= 1500


def test_a_malformed_cache_variable_is_ignored_and_said_once(monkeypatch, caplog):
    """``PPG_PHOTO_CACHE_MB`` is a whole number of megabytes, 0 allowed (the cache of version 1.10); anything else is
    ignored for the default, said once in the log however often the budget is read."""
    monkeypatch.setattr(ortho, "_ignored", set())
    monkeypatch.setenv("PPG_PHOTO_CACHE_MB", "0")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert ortho.photo_cache_budget(4, 8 * 2**30) == (12, 0)
        assert caplog.records == []
        for n, value in enumerate(("abc", "-1", "1.5", "", "1_024"), start=1):
            monkeypatch.setenv("PPG_PHOTO_CACHE_MB", value)
            assert ortho.photo_cache_budget(4, 8 * 2**30) == (12, int(0.35 * 8 * 2**30))
            assert ortho.photo_cache_budget(4, None) == (12, None)
            assert len(caplog.records) == n
            said = caplog.records[-1].getMessage()
            assert "PPG_PHOTO_CACHE_MB" in said and repr(value) in said
        monkeypatch.setenv("PPG_PHOTO_CACHE_MB", " 512 ")
        assert ortho.photo_cache_budget(4, None) == (12, 512 * 2**20)
    assert len(caplog.records) == 5


def test_the_context_sizes_its_cache_on_the_memory_shared_among_its_caches(processed, monkeypatch):
    """Every context takes the budget of the memory for its cache (35 % by default), divided among the caches its
    caller keeps at once (``caches``); the floor of photos stays, and without the memory the floor is all."""
    monkeypatch.delenv("PPG_PHOTO_CACHE_MB", raising=False)
    _doc, ch, _ = processed
    lock = threading.Lock()
    surf = ortho._surface(ch, 0.01, "dem", None, lock)
    cams = list(ch.aligned_cameras)

    def context(memory, **kw):
        monkeypatch.setattr(_env, "physical_memory", lambda: memory)
        return ortho._context(ch, 0.01, cams, surf, None, workers=2, lock=lock, **kw)

    budget = int(0.35 * 16 * 2**30)
    ctx = context(16 * 2**30)
    assert (ctx.cache.max_items, ctx.cache.max_bytes, ctx.uncounted) == (12, budget, set())
    assert context(16 * 2**30, caches=4).cache.max_bytes == budget // 4
    assert context(None).cache.max_bytes is None
    monkeypatch.setenv("PPG_PHOTO_CACHE_MB", "64")
    assert context(16 * 2**30, caches=2).cache.max_bytes == 32 * 2**20
    assert ortho._context(ch, 0.01, cams, surf, None, workers=5, lock=lock).cache.max_items == 15


def test_the_orthomosaic_keeps_its_time_and_the_counts_of_its_photo_cache(processed):
    """The orthomosaic of the chunk (``processed`` builds it) leaves ``meta["timings"]["build_orthomosaic"]`` beside
    the records of the matching and the alignment: its seconds, no stages, and what its photo cache did with the
    sizes it had - the budget of this computer."""
    _doc, ch, _ = processed
    record = ch.meta["timings"]["build_orthomosaic"]
    assert set(record) == {"seconds", "stages", "photo_cache"} and record["seconds"] > 0 and record["stages"] == {}
    counts = record["photo_cache"]
    assert set(counts) == {"hits", "reads", "rereads", "max_items", "max_bytes", "peak_bytes"}
    assert counts["reads"] > 0 and counts["hits"] >= 0 and 0 <= counts["rereads"] <= counts["reads"]
    assert counts["max_items"] >= 12 and counts["peak_bytes"] > 0
    assert counts["max_bytes"] == ortho.photo_cache_budget(1, _env.physical_memory())[1]


def _sentinel():
    return {"seconds": 1.0, "stages": {}, "photo_cache": {"hits": 1, "reads": 1, "rereads": 0, "max_items": 12,
                                                         "max_bytes": None, "peak_bytes": 300}}


def test_only_the_orthomosaic_that_becomes_the_product_replaces_the_record(processed, tmp_path, monkeypatch):
    """A build to a file of its own, a cancelled build and the preview of an area leave the record of the product
    as it is; a build of the product replaces it, and the project keeps it."""
    monkeypatch.delenv("PPG_PHOTO_CACHE_MB", raising=False)
    doc, ch = _copy(processed, tmp_path)
    ch.meta["timings"]["build_orthomosaic"] = _sentinel()
    ortho.build_orthomosaic(ch, resolution=0.008, out_path=tmp_path / "aside.tif")
    prog = Progress()

    def stop(fraction, _text=""):
        if fraction > 0.1:
            prog.cancel_requested = True

    prog.callback = stop
    with pytest.raises(Cancelled):
        ortho.build_orthomosaic(ch, resolution=0.004, progress=prog)
    with rasterio.open(ch.orthomosaic_path) as src:
        origin = (src.transform.c, src.transform.f)
    assert ortho.render_area(ch, origin, (64, 64), 0.008).shape == (4, 64, 64)
    assert ch.meta["timings"]["build_orthomosaic"] == _sentinel()
    ortho.build_orthomosaic(ch, resolution=0.008)
    record = ch.meta["timings"]["build_orthomosaic"]
    assert record != _sentinel() and record["seconds"] > 0 and record["photo_cache"]["reads"] > 0
    doc.save()
    assert ppg.Document.open(doc.path).chunk.meta["timings"]["build_orthomosaic"] == record


def test_the_cache_of_version_1_10_reads_photos_again_that_the_budget_keeps(processed, tmp_path, monkeypatch):
    """``PPG_PHOTO_CACHE_MB=0`` leaves the floor of photos alone (the cache of version 1.10): on the trench, photos
    are read again; with a budget that holds them all none is, and the pixels are the same - the baseline of the
    plan against the cache, judged by these counts."""
    doc, ch = _copy(processed, tmp_path)

    def build(megabytes):
        monkeypatch.setenv("PPG_PHOTO_CACHE_MB", megabytes)
        path = ortho.build_orthomosaic(ch, resolution=0.004, tile=128, workers=1)
        with rasterio.open(path) as src:
            return src.read(), ch.meta["timings"]["build_orthomosaic"]["photo_cache"]

    tight_px, tight = build("0")
    roomy_px, roomy = build("1024")
    assert np.array_equal(tight_px, roomy_px)
    assert (tight["max_items"], tight["max_bytes"], roomy["max_items"], roomy["max_bytes"]) == (12, 0, 12, 2**30)
    assert roomy["rereads"] == 0 < tight["rereads"]
    assert tight["reads"] == roomy["reads"] + tight["rereads"]
    assert tight["hits"] + tight["reads"] == roomy["hits"] + roomy["reads"]
    assert 0 < tight["peak_bytes"] < roomy["peak_bytes"] <= 2**30


def test_the_orthophoto_from_the_texture_keeps_its_time_without_a_photo_cache(tmp_path):
    """A chunk without photos builds its orthophoto from the texture of the model: the record keeps the seconds of
    the step and no photo cache (no photo is read); a build to a file of its own leaves the record alone."""
    doc = new_doc(tmp_path)
    xyz, faces, uv = grid_mesh(0.0, 0.0, 1.0, 1.0, 10, 10, z=lambda x, y: 10.0 + 0.0 * x)  # a local frame
    ch = textured_chunk(doc, "Importato", xyz, faces, uv, [checker(256, 256, (180, 120, 60))])
    ch.meta["timings"] = {"build_orthomosaic": _sentinel()}
    ortho.build_orthomosaic(ch, resolution=0.01, out_path=tmp_path / "aside.tif")
    assert ch.meta["timings"]["build_orthomosaic"] == _sentinel()
    ortho.build_orthomosaic(ch, resolution=0.01)
    assert ch.meta["orthomosaic"]["source"] == "texture"
    record = ch.meta["timings"]["build_orthomosaic"]
    assert set(record) == {"seconds", "stages"} and record["seconds"] > 0 and record["stages"] == {}


def test_the_counts_of_the_tiles_made_by_a_node_are_added_to_the_record(processed, tmp_path, monkeypatch, caplog):
    """A node sends the counts of its cache with each unit of tiles and the main program adds them: the record of an
    orthomosaic shared with a node asks for photos as often as one made alone. A node whose units come without the
    counts (a version without them) leaves the record without a photo cache, with a warning that names it."""
    from threadpoolctl import threadpool_limits

    monkeypatch.setenv("PPG_NODES", "1")
    monkeypatch.delenv("PPG_PHOTO_CACHE_MB", raising=False)
    monkeypatch.setattr(nodes, "ORTHO_UNIT_TILES", 2)  # a small scene: many small units
    monkeypatch.setattr(ortho, "_NODE", {})  # the node is a thread of this process, with a context of its own
    doc, ch = _copy(processed, tmp_path)
    files_dir = doc.files_dir
    sent = []
    real = ortho.node_tiles

    def counted(payload, mapping, out_dir):
        out = real(payload, mapping, out_dir)
        sent.append(out["photo_cache"])
        return out

    def uncounted(payload, mapping, out_dir):
        out = real(payload, mapping, out_dir)
        del out["photo_cache"]
        return out

    def build():
        ortho.build_orthomosaic(ch, resolution=0.004, tile=128, workers=1)
        return ch.meta["timings"]["build_orthomosaic"]

    with threadpool_limits(limits=1, user_api="blas"):  # one BLAS thread for the node and the main program alike
        alone = build()["photo_cache"]
        stop = threading.Event()
        node = threading.Thread(target=nodes.serve, args=(files_dir,), daemon=True,
                                kwargs={"stop_event": stop, "idle": 0.2, "log": lambda *a: None, "host": "altro-1"})
        node.start()
        try:
            for _ in range(50):
                if nodes.present(files_dir):
                    break
                time.sleep(0.1)
            assert nodes.present(files_dir)
            monkeypatch.setattr(ortho, "node_tiles", counted)
            shared = build()["photo_cache"]
            assert sent and sum(c["reads"] for c in sent) > 0
            assert shared["hits"] + shared["reads"] == alone["hits"] + alone["reads"]
            assert shared["max_bytes"] == alone["max_bytes"] and shared["max_items"] == alone["max_items"]
            monkeypatch.setattr(ortho, "node_tiles", uncounted)
            with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
                record = build()
            assert set(record) == {"seconds", "stages"}
            assert any("altro-1" in r.getMessage() and "photo cache" in r.getMessage() for r in caplog.records)
        finally:
            stop.set()
            node.join(120)
    assert not node.is_alive()
