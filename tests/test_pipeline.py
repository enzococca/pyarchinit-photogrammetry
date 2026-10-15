"""End-to-end metric checks on the synthetic trench (EPSG:3004, known targets)."""
import numpy as np
import pytest
import rasterio

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.markers.targets import detect

pytestmark = pytest.mark.slow


def test_alignment_and_georeferencing(processed):
    doc, ch, report = processed
    assert len(ch.aligned_cameras) == len(ch.cameras) == 28
    assert len(ch.markers) == 8
    assert report["method"] == "markers"
    assert report["control_rmse"] < 0.002  # 2 mm


def test_alignment_with_square_pixels(processed):
    """Photos are aligned with one focal length (two can drift apart, see engine/sfm.py);
    the sensor keeps its OPENCV model, also in the saved reconstruction."""
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction

    _doc, ch, _ = processed
    (sensor,) = ch.sensors.values()
    fx, fy, _cx, _cy, k1, _k2, p1, p2 = sensor.params
    assert sensor.model == "OPENCV" and fx == fy and p1 == p2 == 0.0
    assert abs(fx / 1000 - 1) < 0.005 and abs(k1 + 0.04) < 0.005  # the synthetic camera
    (cam,) = load_reconstruction(ch).cameras.values()
    assert cam.model.name == "OPENCV" and list(cam.params) == sensor.params


def test_checkpoints(processed):
    doc, ch, _ = processed
    # use two targets as independent check points
    for label in ("target 4", "target 5"):
        ch.marker(label).checkpoint = True
    report = ch.update_transform()
    assert report["check_rmse"] < 0.003
    for m in ch.markers:
        m.checkpoint = False
    ch.update_transform()


def test_dem_accuracy(processed, scene):
    _, ch, _ = processed
    with rasterio.open(ch.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 4000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
        assert src.crs.to_epsg() == 3004
    err = dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys))
    assert abs(np.median(err)) < 0.003
    assert 1.4826 * np.median(np.abs(err - np.median(err))) < 0.005
    assert np.percentile(np.abs(err), 90) < 0.02


def test_orthomosaic_positions(processed, scene):
    _, ch, _ = processed
    with rasterio.open(ch.orthomosaic_path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
        assert (src.read(4) > 0).mean() > 0.7
    dets = detect(img)
    assert len(dets) >= 6
    for d in dets:
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        assert np.hypot(x - tx, y - ty) < 0.005  # 5 mm


def test_save_reopen_and_exports(processed, tmp_path):
    doc, ch, _ = processed
    doc.save()
    again = ppg.Document.open(doc.path)
    c2 = again.chunk
    assert c2.crs == "EPSG:3004" and len(c2.aligned_cameras) == 28
    assert np.allclose(c2.transform, ch.transform)
    las = c2.export_point_cloud(tmp_path / "cloud.las")
    pc = ppg.PointCloud.load(las)
    assert len(pc) > 50_000 and pc.xyz[:, 0].min() > 2_479_000
    obj = c2.export_model(tmp_path / "model.obj")
    assert obj.exists() and (tmp_path / "model_texture.jpg").exists()
    csv = c2.export_cameras(tmp_path / "cams.csv", fmt="csv")
    assert len(csv.read_text().splitlines()) == 29
    rep = c2.generate_report(tmp_path / "report.html")
    assert "Orthomosaic" in rep.read_text()
    c2.export_orthomosaic(tmp_path / "ortho.tif")
    c2.export_reference(tmp_path / "markers.csv")
