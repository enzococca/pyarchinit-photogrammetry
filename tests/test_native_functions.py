"""Two functions born as console scripts: the reconstruction region on the plane of three markers and the
normal map on the grid of the orthomosaic."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import region as R

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


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


def _three_markers(ch):
    """Three placed markers of the trench that do not lie on a line, and their world positions."""
    placed = [m for m in ch.markers if m.position is not None]
    assert len(placed) >= 3
    for i in range(len(placed) - 2):
        ms = placed[i:i + 3]
        pts = ch.to_world(np.array([m.position for m in ms], float))
        n = np.cross(pts[1] - pts[0], pts[2] - pts[0])
        if np.linalg.norm(n) > 1e-3:
            return ms, pts, n / np.linalg.norm(n)
    raise AssertionError("the placed markers all lie on a line")


def test_region_from_markers_lies_on_the_plane(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    ch.reset_region()
    before = ch.region
    ms, pts, n = _three_markers(ch)
    region = ch.region_from_markers(ms[0].label, ms[1].label, ms[2].label, thickness=0.12)
    assert ch.region is not None and np.allclose(ch.region.rotation, region.rotation)
    axis = region.rotation[:, 2]
    assert abs(abs(axis @ n) - 1) < 1e-6  # the third axis of the box is the normal of the plane
    assert abs((region.center - pts[0]) @ n) < 1e-9  # the plane passes through the centre of the box
    assert np.isclose(region.size[2], 0.12)
    local = region.local(pts)
    assert np.all(np.abs(local[:, :2]) <= region.size[:2] / 2 + 1e-9)  # the footprint covers the three markers
    assert np.all(np.abs(local[:, 2]) < 1e-6)
    # the extent in the plane is the one of the region there was, projected on the (sloping) plane: the
    # box there was is covered, and the new one reaches beyond it at most by its height
    old = before.local(region.corners())
    assert np.all(np.abs(old[:, :2]) <= before.size[:2] / 2 + before.size[2])
    assert region.size[0] >= 0.9 * before.size[0] and region.size[1] >= 0.9 * before.size[1]
    doc.save()
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(doc.path).chunk.region
    assert np.allclose(again.center, region.center) and np.allclose(again.rotation, region.rotation)


def test_region_extent_from_tie_points_when_no_region(processed, tmp_path):
    _doc, ch = _copy(processed, tmp_path)
    ch.region = None
    ms, pts, n = _three_markers(ch)
    region = R.region_from_markers(ch, ms[0].label, ms[1].label, ms[2].label, thickness=0.2)
    local = region.local(ch.to_world(ch.tie_points.xyz))
    inside = np.all(np.abs(local[:, :2]) <= region.size[:2] / 2, axis=1)
    assert inside.mean() > 0.9 and np.isclose(region.size[2], 0.2)
    # the same from the tie points even with a region, and by the code of the targets
    ch.reset_region()
    codes = [str(m.code) for m in ms]
    other = R.region_from_markers(ch, *codes, thickness=0.2, extent="tie_points")
    assert np.allclose(other.size, region.size) and np.allclose(other.center, region.center)
    with pytest.raises(ValueError):
        R.region_from_markers(ch, ms[0].label, ms[1].label, ms[1].label, thickness=0.2)
    with pytest.raises(KeyError):
        R.region_from_markers(ch, "nessuno", ms[1].label, ms[2].label, thickness=0.2)
    for bad in (0, -0.2):  # a box with no thickness would leave every product empty
        with pytest.raises(ValueError):
            R.region_from_markers(ch, ms[0].label, ms[1].label, ms[2].label, thickness=bad)


def _floor(ds):
    """Rows and columns of the orthomosaic grid on the flat floor of the trench (away from the wall stub)."""
    import rasterio.transform

    cols, rows = np.meshgrid(np.arange(ds.width), np.arange(ds.height))
    x, y = rasterio.transform.xy(ds.transform, rows.ravel(), cols.ravel())  # cell centres
    x, y = np.asarray(x).reshape(rows.shape) - ORIGIN[0], np.asarray(y).reshape(rows.shape) - ORIGIN[1]
    return (x > 1.1) & (x < 1.6) & (y > 1.0) & (y < 2.0)


def test_normal_map_has_the_orthomosaic_grid_and_up_normals(processed, tmp_path):
    import rasterio

    from pyarchinit_photogrammetry.engine.normalmap import normal_map_on_grid

    doc, ch = _copy(processed, tmp_path)
    with rasterio.open(ch.orthomosaic_path) as o:
        grid = (o.width, o.height, o.transform, o.crs)
        alpha = o.read(4)
        floor = _floor(o)
    path = ch.build_normal_map(source="dem")
    assert path == ch.product_path("normal_map") == ch.own_product_path("normal_map")
    with rasterio.open(path) as n:
        assert (n.width, n.height, n.transform, n.crs) == grid
        assert n.count == 4 and n.dtypes[0] == "uint8"
        dem_rgba = n.read()
    assert np.all(dem_rgba[3][alpha == 0] == 0)
    assert dem_rgba[3].max() == 255
    assert np.median(dem_rgba[2][floor]) > 240
    pytest.importorskip("open3d")
    model = normal_map_on_grid(ch, source="model", path=tmp_path / "normali_modello.tif")
    with rasterio.open(model) as n:
        assert (n.width, n.height, n.transform, n.crs) == grid
        model_rgba = n.read()
    assert np.median(model_rgba[2][floor]) > 240
    both = floor & (dem_rgba[3] > 0) & (model_rgba[3] > 0)
    a = dem_rgba[:3, both].T / 127.5 - 1
    b = model_rgba[:3, both].T / 127.5 - 1
    cos = (a * b).sum(1) / np.linalg.norm(a, axis=1) / np.linalg.norm(b, axis=1)
    assert np.degrees(np.arccos(np.clip(np.median(cos), -1, 1))) < 5
    # "auto" takes the model when there is one
    auto = ch.build_normal_map()
    with rasterio.open(auto) as n:
        assert np.array_equal(n.read(), model_rgba)
    assert "normal_map" in ch.products
    doc.save()


def _without_crs(ch, name):
    """The product ``name`` of the chunk rewritten without a CRS, as a local chunk has it."""
    import rasterio

    p = ch.product_path(name)
    with rasterio.open(p) as s:
        profile, data, interp = s.profile, s.read(), s.colorinterp
    profile.pop("crs", None)
    q = p.with_name("locale_" + p.name)
    with rasterio.open(q, "w", **profile) as d:
        d.write(data)
        d.colorinterp = interp
    ch._set_product(name, q)


def test_normal_map_on_a_local_chunk(processed, tmp_path):
    """A chunk without a CRS (a wall or an object scaled with bars): DEM and orthomosaic without a
    CRS give a normal map without one, the same cells as the georeferenced chunk."""
    import rasterio

    from pyarchinit_photogrammetry.engine.normalmap import normal_map_on_grid

    _doc, ch = _copy(processed, tmp_path)
    sources = ["dem"]
    try:
        import open3d  # noqa: F401

        sources.append("model")
    except ImportError:
        pass
    reference = {}
    for source in sources:
        with rasterio.open(normal_map_on_grid(ch, source=source, path=tmp_path / f"geo_{source}.tif")) as n:
            reference[source] = n.read()
    for name in ("dem", "orthomosaic"):
        _without_crs(ch, name)
    ch.crs = "LOCAL"
    with rasterio.open(ch.orthomosaic_path) as o:
        assert o.crs is None
        grid = (o.width, o.height, o.transform)
    for source in sources:
        out = normal_map_on_grid(ch, source=source, path=tmp_path / f"locale_{source}.tif")
        with rasterio.open(out) as n:
            assert n.crs is None and (n.width, n.height, n.transform) == grid
            local = n.read()
        assert local[3].max() == 255
        assert np.array_equal(local, reference[source]), source


def test_normal_map_block_by_block(processed, tmp_path, monkeypatch):
    """The grid made and written in blocks of rows: no block larger than the rows asked, one
    window written a block, and the same normal map as in one block (the whole grid at once)."""
    import tracemalloc

    import rasterio

    from pyarchinit_photogrammetry.engine import normalmap as N

    _doc, ch = _copy(processed, tmp_path)
    with rasterio.open(ch.orthomosaic_path) as o:
        h, w = o.height, o.width
    assert h > 4 * 64
    sources = ["dem"]
    try:
        import open3d  # noqa: F401

        sources.append("model")
    except ImportError:
        pass
    encode = N._encode
    for source in sources:
        monkeypatch.setattr(N, "NORMAL_BLOCK_ROWS", 10 ** 6)
        whole = N.normal_map_on_grid(ch, source=source, path=tmp_path / f"intera_{source}.tif")
        monkeypatch.setattr(N, "NORMAL_BLOCK_ROWS", 64)
        blocks = []

        def recording(normals, opaque):
            blocks.append(normals.shape)
            return encode(normals, opaque)

        monkeypatch.setattr(N, "_encode", recording)
        writes = []
        real_open = rasterio.open

        class Counting:
            def __init__(self, ds):
                self.ds = ds

            def __getattr__(self, name):
                return getattr(self.ds, name)

            def __enter__(self):
                self.ds.__enter__()
                return self

            def __exit__(self, *exc):
                return self.ds.__exit__(*exc)

            def write(self, arr, window=None, **kw):
                writes.append((arr.shape, window))
                return self.ds.write(arr, window=window, **kw)

        def opening(fp, mode="r", **kw):
            ds = real_open(fp, mode, **kw)
            return Counting(ds) if mode == "w" else ds

        monkeypatch.setattr(rasterio, "open", opening)
        tracemalloc.start()
        try:
            blocky = N.normal_map_on_grid(ch, source=source, path=tmp_path / f"blocchi_{source}.tif")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            monkeypatch.setattr(rasterio, "open", real_open)
            monkeypatch.setattr(N, "_encode", encode)
        assert len(blocks) == len(writes) == -(-h // 64)
        assert all(shape[0] <= 64 and shape[1] == w for shape in blocks)
        assert all(arr[0] == 4 and arr[1] <= 64 and window is not None for arr, window in writes)
        # the whole grid of float64 normals alone would be h * w * 24 bytes: the blocks take far less
        assert peak < h * w * 24 / 2, (peak, h * w * 24)
        with rasterio.open(whole) as a, rasterio.open(blocky) as b:
            one, many = a.read().astype(int), b.read().astype(int)
            assert b.overviews(1) == a.overviews(1)
        if source == "model":
            assert np.array_equal(one, many)  # the same rays, cast in other tiles
        else:
            # GDAL resamples a window of the grid with the last bit of a float32 height now and then
            # different from the whole grid (13 cells of 3 million on the trench): a level of RGB at most
            assert np.array_equal(one[3], many[3])
            differ = np.abs(one[:3] - many[:3])
            assert differ.max() <= 1 and (differ > 0).mean() < 1e-4, (differ.max(), (differ > 0).sum())


def test_cli_region_and_normal_map(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    ms, pts, n = _three_markers(ch)
    labels = [m.label for m in ms]
    r = CliRunner().invoke(app, ["region-markers", str(doc.path), *labels, "--thickness", "0.1"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    region = ppg.Document.open(doc.path).chunk.region
    assert region is not None and np.isclose(region.size[2], 0.1) and abs((region.center - pts[0]) @ n) < 1e-9
    r = CliRunner().invoke(app, ["normal-map", str(doc.path), "--source", "dem"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    again = ppg.Document.open(doc.path).chunk
    assert "normal_map" in again.products and again.product_path("normal_map").exists()
    assert "normal_map.tif" in r.output
