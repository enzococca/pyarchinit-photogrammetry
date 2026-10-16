"""Regression tests for issues found in review."""
import shutil
from pathlib import Path
import sqlite3

import numpy as np
import pytest
import rasterio
from PIL import Image

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.archaeo import terrain
from pyarchinit_photogrammetry.engine.dem import write_geotiff


def test_exif_orientation_is_ignored(tmp_path):
    """Photos are used as stored (like COLMAP), never auto-rotated by OpenCV."""
    from pyarchinit_photogrammetry.engine.visibility import ImageCache

    img = Image.fromarray(np.random.default_rng(0).integers(0, 255, (300, 400, 3), dtype=np.uint8))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW on display
    path = tmp_path / "rot.jpg"
    img.save(path, exif=exif)
    doc = ppg.Document()
    ch = doc.add_chunk()
    cam = ch.add_photos([path])[0]
    assert (ch.sensor(cam).width, ch.sensor(cam).height) == (400, 300)
    assert ImageCache(ch).get(cam).shape[:2] == (300, 400)


def test_hillshade_direction(tmp_path):
    yy, xx = np.mgrid[0:50, 0:50].astype(float)
    lit = {}
    for name, z in (("nw", (xx + yy) * 0.7), ("se", -(xx + yy) * 0.7)):  # rows grow southwards
        p = write_geotiff(tmp_path / f"{name}.tif", z.astype(np.float32), (0, 50), 1.0, "EPSG:3004")
        with rasterio.open(terrain.hillshade(p, tmp_path / f"{name}_hs.tif")) as src:
            lit[name] = int(src.read(1)[25, 25])
    assert lit["nw"] > 200 > 50 > lit["se"]  # NW-facing slope lit by the default NW sun


def test_volume_polygon_reference(tmp_path):
    from shapely.geometry import box

    z = np.full((100, 200), 10.0, np.float32)
    z[:, 100:] = 5.0
    p = write_geotiff(tmp_path / "d.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)
    res = terrain.volume(p, polygon=box(0.1, 0.1, 0.5, 0.5))  # flat 10 m area only
    assert res["net_m3"] == pytest.approx(0.0) and res["quota_min"] == pytest.approx(10.0)


@pytest.mark.slow
def test_geographic_gcps_become_utm(processed, scene_dir):
    from pyproj import Transformer

    doc, ch, _ = processed
    doc.save()
    copy = ppg.Document.open(doc.path).chunk
    copy.crs = "LOCAL"
    copy.transform = np.eye(4)
    for m in copy.markers:
        m.reference.location = None
    tr = Transformer.from_crs(3004, 4326, always_xy=True)
    lines = []
    for line in (scene_dir / "gcp.csv").read_text().splitlines():
        if line.startswith("#"):
            continue
        code, x, y, z = line.split(",")
        lon, lat = tr.transform(float(x), float(y))
        lines.append(f"{code},{lon:.10f},{lat:.10f},{z}")
    (scene_dir / "gcp_wgs84.csv").write_text("\n".join(lines))
    copy.import_reference(scene_dir / "gcp_wgs84.csv", crs="EPSG:4326")
    assert copy.crs == "EPSG:32633"
    rep = copy.update_transform()
    assert rep["control_rmse"] < 0.003
    assert 0.1 < copy.scale < 10  # metres, not degrees


@pytest.mark.slow
def test_project_is_relocatable(processed, scene_dir, tmp_path):
    doc, _, _ = processed
    doc.save()
    moved = tmp_path / "moved"
    shutil.copytree(scene_dir, moved)
    again = ppg.Document.open(moved / "project.ppg").chunk
    assert all(str(c.path).startswith(str(moved)) for c in again.cameras)
    assert again.meta["sfm"]["image_root"].startswith(str(moved))
    assert again.dem_path.exists()


@pytest.mark.slow
def test_publish_is_idempotent_and_footprint_valid(pa_db, processed, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    _, ch, _ = processed
    pa = PyArchInit.connect()
    pa.publish_orthomosaic(ch, "Saggio", "Orto")
    with pytest.raises(FileExistsError):
        pa.publish_orthomosaic(ch, "Saggio", "Orto")
    again = pa.publish_orthomosaic(ch, "Saggio", "Orto", overwrite=True, link_us=[("1", "1")])
    assert again["id_media"] == 1
    con = sqlite3.connect(pa_db)
    from conftest import load_spatialite

    if not load_spatialite(con):

        pytest.skip("mod_spatialite not available")
    assert con.execute("SELECT count(*) FROM media_table").fetchone()[0] == 1
    wkt = con.execute("SELECT AsText(the_geom) FROM pyarchinit_documentazione").fetchone()[0]
    assert wkt and wkt.startswith("LINESTRING")
    x0, y0 = 2480000.0, 4500000.0
    assert pa.publish_elevations(ch, "Saggio", "1", "1", [(x0 - 5.0, y0 + 1.0)]) == []


def test_spatialite_before_pycolmap_keeps_jpeg_decoding(tmp_path):
    """Loading SpatiaLite (system libjpeg) must not break COLMAP's JPEG reader."""
    import subprocess
    import sys
    import textwrap

    import cv2

    img = tmp_path / "a.jpg"
    cv2.imwrite(str(img), (np.random.default_rng(0).random((64, 64, 3)) * 255).astype(np.uint8))
    code = textwrap.dedent(f"""
        import sqlite3, sys
        from pyarchinit_photogrammetry.pyarchinit.database import _warm_up_proj, _spatialite_paths
        _warm_up_proj()
        con = sqlite3.connect(":memory:")
        con.enable_load_extension(True)
        for p in _spatialite_paths():
            try:
                con.load_extension(p)
                break
            except Exception:
                continue
        else:
            print("SKIP"); sys.exit(0)
        import pycolmap
        print("OK" if pycolmap.Bitmap.read({str(img)!r}, True) is not None else "FAIL")
    """)
    # close_fds=False: posix_spawn instead of fork (a fork of this process crashes on macOS once
    # SpatiaLite and rasterio/PROJ are loaded, see engine/isolate.py)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         close_fds=False).stdout
    if "SKIP" in out:
        pytest.skip("mod_spatialite not available")
    assert "OK" in out, out


def test_foreign_proj_lib_does_not_break_epsg_lookup(tmp_path):
    """Anaconda/QGIS export PROJ_LIB pointing at an older proj.db: EPSG codes must still resolve."""
    import os
    import subprocess
    import sys

    bundled = Path(rasterio.__file__).parent / "proj_data" / "proj.db"
    if not bundled.exists():
        pytest.skip("rasterio without bundled PROJ data (conda build)")
    old = tmp_path / "proj"
    old.mkdir()
    shutil.copy(bundled, old / "proj.db")
    con = sqlite3.connect(old / "proj.db")
    con.execute("UPDATE metadata SET value='2' WHERE key='DATABASE.LAYOUT.VERSION.MINOR'")
    con.commit()
    con.close()
    code = ("import pyarchinit_photogrammetry\nfrom rasterio.crs import CRS\n"
            "print('OK' if 'Monte Mario' in CRS.from_epsg(3004).to_wkt() else 'FAIL')")
    env = dict(os.environ, PROJ_LIB=str(old))
    env.pop("PROJ_DATA", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120,
                         close_fds=False)
    assert "OK" in out.stdout, out.stderr[-2000:]


def test_mapping_camera_has_square_pixels_unless_calibrated():
    from pyarchinit_photogrammetry.core.camera import Sensor
    from pyarchinit_photogrammetry.engine.sfm import mapping_camera, sensor_model_params

    s = Sensor(key=0, label="c", width=1200, height=900)  # OPENCV, fx = fy, no distortion
    model, params = mapping_camera(s)
    assert model == "RADIAL" and params == [1440.0, 600.0, 450.0, 0.0, 0.0]
    assert sensor_model_params("RADIAL", [1100.0, 601.0, 449.0, 0.01, -0.002], "OPENCV") == (
        "OPENCV", [1100.0, 1100.0, 601.0, 449.0, 0.01, -0.002, 0.0, 0.0])
    pin = Sensor(key=1, label="p", width=1200, height=900, model="PINHOLE")
    assert mapping_camera(pin)[0] == "SIMPLE_PINHOLE"
    assert sensor_model_params("SIMPLE_PINHOLE", [900.0, 600.0, 450.0], "PINHOLE") == (
        "PINHOLE", [900.0, 900.0, 600.0, 450.0])
    for params in ([1100.0, 1101.0, 600.0, 450.0, 0, 0, 0, 0],  # a calibration: kept as it is
                   [1100.0, 1100.0, 600.0, 450.0, 0, 0, 1e-4, 0]):
        cal = Sensor(key=2, label="k", width=1200, height=900, params=params)
        assert mapping_camera(cal) == ("OPENCV", [float(v) for v in params])


def test_optimize_cameras_on_control_points(processed, tmp_path):
    """The control points take part in the adjustment (fixed points observed in the photos):
    the block bends towards them, check points stay out and are reported before/after."""
    import shutil

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    doc, _ch, _ = processed

    def copy(name):
        d = tmp_path / name
        d.mkdir()
        shutil.copy2(doc.path, d / doc.path.name)
        shutil.copytree(doc.files_dir, d / doc.files_dir.name)
        out = ppg.Document.open(d / doc.path.name).chunk
        for m in out.markers:
            if m.label in ("target 2", "target 5"):
                m.checkpoint = True
        off = next(m for m in out.markers if m.label == "target 0")  # measured 1 cm off on purpose
        off.reference.location = list(np.asarray(off.reference.location, float) + [0.010, 0.0, 0.0])
        return out

    shifted = {}
    for mode in ("plain", "markers"):
        ch = copy(mode)
        err = ch.optimize_cameras(use_markers=mode == "markers")
        rows = {r["label"]: r for r in gcp.update_transform(ch)["markers"]}
        shifted[mode] = rows["target 0"]["error"]
        assert err < 0.5
        checks = [rows[k]["error"] for k in ("target 2", "target 5")]
        assert max(checks) < 0.003, checks
        report = ch.meta["sfm"]["optimize"]
        if mode == "markers":
            assert report["control_points"] == len(ch.markers) - 2  # check points are not used
            assert report["before"]["check"] is not None and report["after"]["check"] < 0.003
        else:
            assert report["control_points"] == 0
    assert shifted["markers"] < shifted["plain"] - 0.0002  # the block bent towards the control point


def test_model_from_imported_poses_refers_only_to_written_points(processed, tmp_path):
    """The COLMAP model written from imported poses (Metashape, WebODM, merged chunks) must
    not refer to points seen by a single photo: they are not written, and the depth maps
    failed with KeyError on them. A model written that way by earlier versions is repaired."""
    import pycolmap

    from pyarchinit_photogrammetry.engine import dense, sfm

    doc0, _, _ = processed
    src = Path(doc0.path)
    shutil.copytree(src.parent, tmp_path / "copia", ignore=shutil.ignore_patterns("dense"))
    ch = ppg.Document.open(tmp_path / "copia" / src.name).chunks[0]
    out = sfm.write_reconstruction_from_chunk(ch, max_obs_per_image=60)  # many points seen once
    rec = sfm.load_reconstruction(ch)
    refs = [p.point3D_id for img in rec.images.values() for p in img.points2D if p.has_point3D()]
    assert refs and all(rec.exists_point3D(i) for i in refs)
    assert all(len(p.track.elements) >= 2 for p in rec.points3D.values())
    # an old model with a dangling reference: repaired, and the neighbours of the depth maps work
    txt = out.parent / "model_txt"
    lines = (txt / "images.txt").read_text().splitlines(keepends=True)
    lines[1] = lines[1].rstrip("\n") + " 10.5 10.5 999999\n"
    (txt / "images.txt").write_text("".join(lines))
    broken = pycolmap.Reconstruction()
    broken.read_text(str(txt))
    broken.write(str(out))
    assert sfm.repair_reconstruction(ch) == 1 and sfm.repair_reconstruction(ch) == 0
    assert dense._neighbors(sfm.load_reconstruction(ch))


def test_depth_maps_with_photos_kept_online_only(processed, tmp_path):
    """Dropbox/iCloud keep "online only" photos as empty files: the depth maps say so, and
    with skip_unreadable they are built from the other photos."""
    from pyarchinit_photogrammetry.engine import dense

    doc0, _, _ = processed
    src = Path(doc0.path)
    shutil.copytree(src.parent, tmp_path / "copia", ignore=shutil.ignore_patterns("dense"))
    doc = ppg.Document.open(tmp_path / "copia" / src.name)
    ch = doc.chunks[0]
    empty = ch.aligned_cameras[3]
    Path(empty.path).write_bytes(b"")
    assert [c.key for c in dense.unreadable_photos(ch)] == [empty.key]
    with pytest.raises(dense.UnreadablePhotos, match="1 photos cannot be read"):
        ch.build_depth_maps(quality="low")
    ch.meta["point_cloud_source"] = "vertici del modello Metashape"  # as after an import
    ch.build_depth_maps(quality="low", skip_unreadable=True)
    assert ch.meta["dense"]["skipped_photos"] == [empty.label]
    assert len(ch.build_point_cloud()) > 1000
    assert ch.meta["point_cloud_source"] == "mappe di profondita' (qualita' low)"
