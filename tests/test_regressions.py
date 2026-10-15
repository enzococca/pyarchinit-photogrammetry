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
