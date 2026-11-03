"""Two functions born as console scripts: the reconstruction region on the plane of three markers and the
normal map on the grid of the orthomosaic."""
from __future__ import annotations

import shutil
from pathlib import Path

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


# ---------------------------------------------------------------- duplicate a chunk, enable or disable photos
def _six(ch):
    """Six nadir photos of the trench in a block of two rows by three (inside the grid of 4 x 5): each has
    partners among the others for its depth map."""
    return [ch.camera(f"IMG_{i:04d}") for i in (6, 7, 8, 11, 12, 13)]


def _snapshot(ch):
    import json

    return json.dumps(ch.to_dict(), sort_keys=True, default=str)


def test_duplicate_chunk_with_some_photos_rebuilds_depth_maps(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.confidence import depth_maps

    doc, ch = _copy(processed, tmp_path)
    assert len(ch.cameras) == 28
    before = _snapshot(ch)
    files_before = sorted(p.name for p in ch.data_dir.iterdir())
    six = _six(ch)
    keys = {c.key for c in six}
    dup = doc.duplicate_chunk(ch, six, label="Sei foto")
    assert dup in doc.chunks and dup is not ch and dup.key != ch.key and dup.label == "Sei foto"
    assert dup.data_dir != ch.data_dir
    assert len(dup.cameras) == 6 and {c.key for c in dup.cameras} == keys
    assert all(c.aligned for c in dup.cameras) and all(c is not s for c in dup.cameras for s in six)
    for c in dup.cameras:  # the same poses
        assert np.allclose(c.transform, ch.camera(c.key).transform)
    assert set(dup.sensors) == {c.sensor_key for c in six}
    assert [m.label for m in dup.markers] == [m.label for m in ch.markers]
    assert sum(len(m.projections) for m in dup.markers) < sum(len(m.projections) for m in ch.markers)
    assert all(set(m.projections) <= keys for m in dup.markers)
    assert np.allclose(dup.transform, ch.transform) and dup.crs == ch.crs
    for name in ("point_cloud", "model", "dem", "orthomosaic", "tie_points"):
        assert name not in dup.products  # products never copied; tie points only with every photo
    for key in ("orthomosaic", "dense", "versions", "drawings", "proxies"):
        assert key not in dup.meta
    assert dup.product_path("reconstruction").exists()
    dup.build_depth_maps(quality="low")
    names = dup.meta["sfm"]["names"]
    assert sorted(e["name"] for e in depth_maps(dup)) == sorted(names[str(k)] for k in keys)
    assert _snapshot(ch) == before  # the source chunk untouched
    assert sorted(p.name for p in ch.data_dir.iterdir()) == files_before
    doc.save()
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(doc.path)
    assert [len(c.cameras) for c in again.chunks] == [28, 6]


def test_duplicate_chunk_all_photos_keeps_tie_points(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    ch.cameras[3].enabled = False
    dup = doc.duplicate_chunk(ch)
    assert dup.label == f"{ch.label} (copia)"
    second = doc.duplicate_chunk(ch)  # the default names stay unique
    assert second.label == f"{ch.label} (copia 2)"
    assert len(dup.cameras) == len(ch.cameras)
    assert [c.enabled for c in dup.cameras] == [c.enabled for c in ch.cameras]
    assert dup.product_path("tie_points").exists()
    assert dup.product_path("tie_points").parent == dup.data_dir
    assert len(dup.tie_points) == len(ch.tie_points)
    assert sum(len(m.projections) for m in dup.markers) == sum(len(m.projections) for m in ch.markers)
    assert len(dup.scalebars) == len(ch.scalebars)
    assert not {"point_cloud", "model", "dem", "orthomosaic"} & set(dup.products)
    with pytest.raises(ValueError):  # a camera of another chunk
        doc.duplicate_chunk(dup, [ch.cameras[0]])


def test_set_cameras_enabled_and_builders_honour_it(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.confidence import depth_maps

    _doc, ch = _copy(processed, tmp_path)
    six = {c.key for c in _six(ch)}
    others = [c for c in ch.cameras if c.key not in six]
    assert len(others) == 22
    # by object, by key and by label
    mixed = others[:8] + [c.key for c in others[8:15]] + [c.label for c in others[15:]]
    assert ch.set_cameras_enabled(mixed, False) == 22
    assert ch.set_cameras_enabled(mixed, False) == 0  # nothing changes the second time
    assert {c.key for c in ch.aligned_cameras} == six
    with pytest.raises(KeyError):
        ch.set_cameras_enabled(["nessuna foto"], False)
    copy = _doc.duplicate_chunk(ch, [ch.camera(k) for k in sorted(six)[:3]])  # the source's keys, other objects
    with pytest.raises(ValueError):
        ch.set_cameras_enabled(copy.cameras[:1], False)
    assert copy.set_cameras_enabled(copy.cameras[:1], False) == 1  # its own: fine
    assert ch.camera(copy.cameras[0].key).enabled  # the source's photo with that key untouched
    _doc.remove_chunk(copy)
    seen = []
    ch.build_orthomosaic(resolution=0.05, progress=lambda f, m: seen.append(m or ""))
    assert ch.meta["orthomosaic"]["photos"] == 6
    assert any("from 6 photos" in m for m in seen)
    ch.build_depth_maps(quality="low")
    names = ch.meta["sfm"]["names"]
    assert sorted(e["name"] for e in depth_maps(ch)) == sorted(names[str(k)] for k in six)  # the enabled ones
    assert ch.set_cameras_enabled(others, True) == 22 and len(ch.aligned_cameras) == 28


def test_cli_duplicate_chunk_and_photos(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    six = _six(ch)
    names = tmp_path / "foto.txt"
    names.write_text("\n".join(c.label for c in six) + "\n")
    r = CliRunner().invoke(app, ["duplicate-chunk", str(doc.path), "--photos", str(names), "--label", "Sei"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    again = ppg.Document.open(doc.path)
    assert [c.label for c in again.chunks] == [ch.label, "Sei"] and len(again.chunks[1].cameras) == 6
    r = CliRunner().invoke(app, ["photos", "disable", str(doc.path), six[0].label, six[1].label, "--chunk", "Sei"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    again = ppg.Document.open(doc.path)
    assert sum(not c.enabled for c in again.chunks[1].cameras) == 2
    assert all(c.enabled for c in again.chunks[0].cameras)
    r = CliRunner().invoke(app, ["photos", "enable", str(doc.path), six[0].label, "--chunk", "Sei"])
    assert r.exit_code == 0 and "1" in r.output
    r = CliRunner().invoke(app, ["photos", "disable", str(doc.path), "nessuna"])
    assert r.exit_code == 2
    r = CliRunner().invoke(app, ["duplicate-chunk", str(doc.path), "--area", "POLYGON((0 0, 1 0, 1 1, 0 0))"])
    assert r.exit_code == 2  # an area far from the photos: no photo
    r = CliRunner().invoke(app, ["duplicate-chunk", str(doc.path), "--area", "non un poligono"])
    assert r.exit_code == 2


# ---------------------------------------------------------------- the normal map: what falls back to the DEM
def test_normal_map_auto_falls_back_on_a_model_that_cannot_be_read(processed, tmp_path, monkeypatch):
    pytest.importorskip("open3d")
    from pyarchinit_photogrammetry.io import mesh_ply

    _doc, ch = _copy(processed, tmp_path)

    def broken(*_a, **_k):
        raise ValueError("not a PLY")

    monkeypatch.setattr(mesh_ply, "read_mesh", broken)
    ch.build_normal_map()
    assert ch.meta["normal_map"]["source"] == "dem"
    with pytest.raises(ValueError):
        ch.build_normal_map(source="model")


def test_normal_map_of_a_model_covering_no_cell(processed, tmp_path, monkeypatch):
    pytest.importorskip("open3d")
    import rasterio

    from pyarchinit_photogrammetry.engine import normalmap as N

    _doc, ch = _copy(processed, tmp_path)

    def nowhere(self, r0, r1):
        return np.full((r1 - r0, self.shape[1], 3), np.nan, np.float32)

    monkeypatch.setattr(N._ModelNormals, "block", nowhere)
    path = N.normal_map_on_grid(ch, source="model", path=tmp_path / "vuota.tif")
    with rasterio.open(path) as n:
        assert n.read(4).max() == 0  # transparent everywhere
    ch.build_normal_map()  # "auto": the DEM instead
    assert ch.meta["normal_map"]["source"] == "dem"
    with rasterio.open(ch.product_path("normal_map")) as n:
        assert n.read(4).max() == 255


def test_cli_normal_map_bad_parameters(processed, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    r = CliRunner().invoke(app, ["normal-map", str(doc.path), "--source", "lidar"])
    assert r.exit_code == 2 and "auto, dem or model" in r.output
    ch.products.pop("orthomosaic")
    doc.save()
    r = CliRunner().invoke(app, ["normal-map", str(doc.path)])
    assert r.exit_code == 2 and "orthomosaic" in r.output


def test_duplicate_chunk_keeps_masks_and_its_own_capture_copies(processed, tmp_path):
    """Masks are settings of the photos (copied for the photos kept); reference copies of multispectral or thermal
    captures living in the source's folder are copied into the duplicate's folder and the cameras point there; the
    settings of the captures stay, their products do not."""
    import json

    from pyarchinit_photogrammetry.engine.masks import mask_dir, mask_path

    doc, ch = _copy(processed, tmp_path)
    six = _six(ch)
    masks = mask_dir(ch)
    masks.mkdir(parents=True, exist_ok=True)
    for c in (six[0], ch.camera("IMG_0000")):  # one kept, one left out
        (masks / f"{c.key}.png").write_bytes(b"mask")
    reference = ch.data_dir / "multispectral" / "reference"
    reference.mkdir(parents=True)
    for c in six[:2]:  # two photos as reference copies inside the chunk folder
        target = reference / (c.label + ".jpg")
        shutil.copy2(c.path, target)
        c.path = str(target)
    ch.meta["multispectral"] = {"reference": "Red", "bands": ["Red", "NIR"], "captures": 6, "panel": None,
                                "registration": {"a": 1}, "reflectance": "multispectral/reflectance.tif",
                                "reflectance_bands": ["Red", "NIR"], "indices": {"NDVI": {"file": "x.tif"}},
                                "zoning": {"NDVI": {"raster": "z.tif"}}}
    ch.meta["thermal"] = {"params": {"emissivity": 0.95}, "file": "multispectral/thermal.tif", "resolution": 0.03,
                          "stats": {}, "anomaly": {"file": "a.tif"}, "decoded": 4, "registration": {}}
    before = json.dumps(ch.to_dict(), sort_keys=True, default=str)
    dup = doc.duplicate_chunk(ch, six)
    assert json.dumps(ch.to_dict(), sort_keys=True, default=str) == before
    assert mask_path(dup, dup.camera(six[0].key)) is not None
    assert not (mask_dir(dup) / f"{ch.camera('IMG_0000').key}.png").exists()
    for c in six[:2]:
        new = dup.camera(c.key)
        assert Path(new.path).parent == dup.data_dir / "multispectral" / "reference"
        assert Path(new.path).exists() and Path(c.path).exists()
    assert Path(dup.camera(six[2].key).path) == Path(six[2].path)  # a photo outside the chunk folder: as it is
    ms, th = dup.meta["multispectral"], dup.meta["thermal"]
    assert ms["bands"] == ["Red", "NIR"] and ms["registration"] == {"a": 1}
    assert not {"reflectance", "reflectance_bands", "indices", "zoning"} & set(ms)
    assert th["params"] == {"emissivity": 0.95} and not {"file", "resolution", "stats", "anomaly", "decoded"} & set(th)


def test_duplicate_of_a_subset_needs_tie_points(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    ch.products.pop("tie_points")
    ch._tie_points = None
    n = len(doc.chunks)
    with pytest.raises(ValueError, match="tie points"):
        doc.duplicate_chunk(ch, _six(ch))
    assert len(doc.chunks) == n
