"""Satellite scenes over the survey: RPCs read from the GeoTIFF and from the sidecars, the
orthorectification on the DEM (against the orthomosaic of the same ground; a flat ground shifts
the floor of the trench along the line of sight), a surface model from a stereo pair (against the
DEM) - the synthetic trench seen by two "satellites" built as parallel projections."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import satellite as sat

THETA = 25.0  # off-nadir angle (degrees)


def _affine_rpc(crs: str, bounds, h0: float, theta: float, azimuth: float, swing: float, gsd: float,
                width: int, height: int):
    """The RPC of a parallel-projection satellite: the ground point (x, y, h) is seen where the
    plane ``h0`` shows ``(x, y) - (h - h0) tan(theta) (sin az, cos az)`` (a tower leans away from
    the satellite); image axes turned by ``swing``. Returns the RPC and the exact mapping."""
    from pyproj import Transformer

    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    to_xy = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xc, yc = 0.5 * (bounds[0] + bounds[2]), 0.5 * (bounds[1] + bounds[3])
    t, s, c = np.tan(np.radians(theta)), np.sin(np.radians(azimuth)), np.cos(np.radians(azimuth))
    ps, pc = np.sin(np.radians(swing)), np.cos(np.radians(swing))
    samp_off, line_off = (width - 1) / 2, (height - 1) / 2

    def image(x, y, h):
        ux = x - (h - h0) * t * s - xc
        uy = y - (h - h0) * t * c - yc
        return (pc * ux + ps * uy) / gsd + samp_off, (ps * ux - pc * uy) / gsd + line_off

    lon_c, lat_c = to_ll.transform(xc, yc)
    lon0, lat0 = to_ll.transform(bounds[0], bounds[1])
    lon1, lat1 = to_ll.transform(bounds[2], bounds[3])
    long_scale, lat_scale, height_scale = abs(lon1 - lon0), abs(lat1 - lat0), 2.0
    L, P, H = np.meshgrid(np.linspace(-1, 1, 7), np.linspace(-1, 1, 7), np.linspace(-1, 1, 5), indexing="ij")
    x, y = to_xy.transform(lon_c + L * long_scale, lat_c + P * lat_scale)
    col, row = image(x, y, h0 + H * height_scale)
    A = np.stack([np.ones_like(L), L, P, H], -1).reshape(-1, 4)
    samp_scale, line_scale = max(width / 2, 1.0), max(height / 2, 1.0)
    cs = np.linalg.lstsq(A, (col.ravel() - samp_off) / samp_scale, rcond=None)[0]
    ls = np.linalg.lstsq(A, (row.ravel() - line_off) / line_scale, rcond=None)[0]
    num_s, num_l, den = np.zeros(20), np.zeros(20), np.zeros(20)
    num_s[:4], num_l[:4], den[0] = cs, ls, 1.0
    rpc = sat.Rpc(line_off=line_off, line_scale=line_scale, samp_off=samp_off, samp_scale=samp_scale, lat_off=lat_c,
                  lat_scale=lat_scale, long_off=lon_c, long_scale=long_scale, height_off=h0, height_scale=height_scale,
                  line_num=num_l, line_den=den.copy(), samp_num=num_s, samp_den=den.copy(), err_bias=0.1, err_rand=0.05)
    return rpc, image, (to_ll, to_xy)


def _render(rpc: sat.Rpc, to_xy, ortho_path: Path, dem_path: Path, width: int, height: int, out: Path,
            sidecar: bool = False) -> Path:
    """The scene a satellite with this RPC takes of the orthomosaic draped on the DEM: every
    pixel's ray intersected with the DEM (fixed-point iterations on the height), the orthomosaic
    sampled there; 16-bit, no data 0; the RPC in the file or in a ``_RPC.TXT`` beside it."""
    import rasterio

    dem = sat._Dem(dem_path)
    with rasterio.open(ortho_path) as src:
        rgb = src.read([1, 2, 3]).astype(np.float32)
        alpha = src.read(4) if src.count >= 4 else np.full(rgb.shape[1:], 255, np.uint8)
        tr = src.transform
    C, R = np.meshgrid(np.arange(width, dtype=float), np.arange(height, dtype=float))
    h = np.full(C.shape, rpc.height_off)
    for _ in range(10):
        lon, lat = sat.ground(rpc, C, R, h)
        x, y = to_xy.transform(lon, lat)
        z = dem.sample(x, y)
        h = np.where(np.isfinite(z), z, rpc.height_off)
    colf = ((x - tr.c) / tr.a - 0.5).astype(np.float32)
    rowf = ((y - tr.f) / tr.e - 0.5).astype(np.float32)
    scene = np.zeros((3, height, width), np.uint16)
    ok = cv2.remap(alpha, colf, rowf, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
    for b in range(3):
        v = cv2.remap(rgb[b], colf, rowf, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        scene[b] = np.where(ok, v * 200 + 100, 0).astype(np.uint16)
    with rasterio.open(out, "w", driver="GTiff", width=width, height=height, count=3, dtype="uint16", nodata=0,
                       compress="deflate") as dst:
        dst.write(scene)
        if not sidecar:
            dst.update_tags(ns="RPC", **rpc.to_rasterio().to_gdal())
    if sidecar:
        rpc.write_text(out.with_name(out.stem + "_RPC.TXT"))
    return out


@pytest.fixture(scope="module")
def satellites(processed, tmp_path_factory):
    """Two scenes of the trench, 25 degrees off nadir from the east and from the west, at twice
    the resolution of the orthomosaic (a satellite coarser than the drone)."""
    import rasterio

    doc, ch, _ = processed
    folder = tmp_path_factory.mktemp("satellite")
    ortho, dem = ch.orthomosaic_path, ch.dem_path
    with rasterio.open(ortho) as src:
        bounds, res = tuple(float(v) for v in src.bounds), float(src.res[0])
    gsd = 2 * res
    st = sat._Dem(dem).stats()
    h0 = st["median"]
    size = (int(1.3 * (bounds[2] - bounds[0]) / gsd), int(1.3 * (bounds[3] - bounds[1]) / gsd))
    scenes = {}
    for name, azimuth, swing, sidecar in (("east", 90.0, 10.0, False), ("west", 270.0, -5.0, True)):
        rpc, image, (to_ll, to_xy) = _affine_rpc(ch.crs, bounds, h0, THETA, azimuth, swing, gsd, *size)
        path = _render(rpc, to_xy, ortho, dem, size[0], size[1], folder / f"scene_{name}.tif", sidecar=sidecar)
        scenes[name] = {"path": path, "rpc": rpc, "image": image, "to_ll": to_ll, "to_xy": to_xy, "azimuth": azimuth}
    return {"doc": doc, "chunk": ch, "folder": folder, "ortho": ortho, "dem": dem, "bounds": bounds, "res": res,
            "gsd": gsd, "h0": h0, "scenes": scenes}


def _same(a: sat.Rpc, b: sat.Rpc) -> bool:
    da, db = a.to_dict(), b.to_dict()
    return all(np.allclose(da[k], db[k], rtol=1e-9, atol=1e-12) for k in da if da[k] is not None and db[k] is not None)


def test_rpc_projection_inverse_and_files(satellites, tmp_path):
    s = satellites["scenes"]["east"]
    rpc, image, to_ll, to_xy = s["rpc"], s["image"], s["to_ll"], s["to_xy"]
    b = satellites["bounds"]
    rng = np.random.default_rng(1)
    x = rng.uniform(b[0], b[2], 200)
    y = rng.uniform(b[1], b[3], 200)
    h = satellites["h0"] + rng.uniform(-1, 1, 200)
    lon, lat = to_ll.transform(x, y)
    col, row = sat.project(rpc, lon, lat, h)
    xr, yr = to_xy.transform(lon, lat)  # the datum shift of EPSG:3004 does not come back exactly (0.4 mm)
    c_true, r_true = image(xr, yr, h)
    assert np.abs(col - c_true).max() < 0.01 and np.abs(row - r_true).max() < 0.01  # the fit of the affine
    lon2, lat2 = sat.ground(rpc, col, row, h)
    col2, row2 = sat.project(rpc, lon2, lat2, h)
    assert np.abs(col2 - col).max() < 1e-5 and np.abs(row2 - row).max() < 1e-5
    # the files: RPC in the GeoTIFF, RPC00B text, RPB, DIMAP XML (1-based)
    assert _same(sat.read_rpc(s["path"]), rpc)
    west = satellites["scenes"]["west"]
    assert _same(sat.read_rpc(west["path"]), west["rpc"])
    rpb = tmp_path / "scene.RPB"
    coef = lambda a: "( " + ", ".join(f"{v:+.12E}" for v in a) + " )"  # noqa: E731
    rpb.write_text(f"""satId = "XX";
BEGIN_GROUP = IMAGE
  errBias = 0.1;
  errRand = 0.05;
  lineOffset = {rpc.line_off};
  sampOffset = {rpc.samp_off};
  latOffset = {rpc.lat_off};
  longOffset = {rpc.long_off};
  heightOffset = {rpc.height_off};
  lineScale = {rpc.line_scale};
  sampScale = {rpc.samp_scale};
  latScale = {rpc.lat_scale};
  longScale = {rpc.long_scale};
  heightScale = {rpc.height_scale};
  lineNumCoef = {coef(rpc.line_num)};
  lineDenCoef = {coef(rpc.line_den)};
  sampNumCoef = {coef(rpc.samp_num)};
  sampDenCoef = {coef(rpc.samp_den)};
END_GROUP = IMAGE
END;
""")
    assert _same(sat.read_rpc(s["path"], rpb), rpc)
    tags = "".join(f"<{n}_{i + 1}>{v:.15E}</{n}_{i + 1}>" for n, arr in
                   (("LINE_NUM_COEFF", rpc.line_num), ("LINE_DEN_COEFF", rpc.line_den),
                    ("SAMP_NUM_COEFF", rpc.samp_num), ("SAMP_DEN_COEFF", rpc.samp_den)) for i, v in enumerate(arr))
    xml = tmp_path / "RPC_scene.XML"
    xml.write_text(f"""<?xml version="1.0"?><Dimap_Document><Rational_Function_Model><Global_RFM>
<Direct_Model><SAMP_NUM_COEFF_1>9</SAMP_NUM_COEFF_1></Direct_Model>
<Inverse_Model>{tags}<ERR_BIAS_ROW>0.06</ERR_BIAS_ROW><ERR_BIAS_COL>0.08</ERR_BIAS_COL></Inverse_Model>
<RFM_Validity><LONG_SCALE>{rpc.long_scale}</LONG_SCALE><LONG_OFF>{rpc.long_off}</LONG_OFF>
<LAT_SCALE>{rpc.lat_scale}</LAT_SCALE><LAT_OFF>{rpc.lat_off}</LAT_OFF><HEIGHT_SCALE>{rpc.height_scale}</HEIGHT_SCALE>
<HEIGHT_OFF>{rpc.height_off}</HEIGHT_OFF><SAMP_SCALE>{rpc.samp_scale}</SAMP_SCALE><SAMP_OFF>{rpc.samp_off + 1}</SAMP_OFF>
<LINE_SCALE>{rpc.line_scale}</LINE_SCALE><LINE_OFF>{rpc.line_off + 1}</LINE_OFF></RFM_Validity>
</Global_RFM></Rational_Function_Model></Dimap_Document>""")
    from_xml = sat.read_rpc(s["path"], xml)
    assert from_xml.samp_off == pytest.approx(rpc.samp_off) and from_xml.line_off == pytest.approx(rpc.line_off)
    assert from_xml.err_bias == pytest.approx(0.1) and np.allclose(from_xml.samp_num, rpc.samp_num)
    info = sat.scene_info(s["path"], satellites["chunk"].crs)
    assert info["gsd"] == pytest.approx(satellites["gsd"], rel=0.02) and info["bands"] == 3 and info["dtype"] == "uint16"
    assert len(info["footprint"]) == 5
    with pytest.raises(ValueError):
        sat.read_rpc(satellites["ortho"])


def _reference_on(ortho: Path, like: Path) -> tuple[np.ndarray, np.ndarray]:
    """The orthomosaic (grey) and its alpha resampled on the grid of another raster."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    with rasterio.open(like) as dst, rasterio.open(ortho) as src:
        out = np.zeros((4, dst.height, dst.width), np.float32)
        reproject(source=src.read([1, 2, 3, 4]).astype(np.float32), destination=out, src_transform=src.transform,
                  src_crs=src.crs, dst_transform=dst.transform, dst_crs=dst.crs, resampling=Resampling.bilinear)
    return out[:3].mean(axis=0), out[3] > 250


def test_orthorectification_matches_the_orthomosaic(satellites, tmp_path):
    import rasterio

    s = satellites["scenes"]["east"]
    out = tmp_path / "east_ortho.tif"
    r = sat.orthorectify(s["path"], satellites["dem"], out, resolution=satellites["gsd"])
    assert r["bands"] == 3 and r["dtype"] == "uint16" and r["filled"] > 0.5
    with rasterio.open(out) as src:
        assert src.count == 3 and src.dtypes[0] == "uint16" and src.nodata == 0
        data = src.read().astype(np.float32)
        assert src.crs.to_epsg() == 3004 and src.res[0] == pytest.approx(satellites["gsd"])
    grey = data.mean(axis=0)
    ref, ref_ok = _reference_on(satellites["ortho"], out)
    ok = (data > 0).all(axis=0) & ref_ok
    assert ok.mean() > 0.5
    assert np.corrcoef(grey[ok], ref[ok])[0, 1] > 0.9
    a = np.where(ok, grey - grey[ok].mean(), 0).astype(np.float32)
    b = np.where(ok, ref - ref[ok].mean(), 0).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(a, b)
    assert abs(dx) < 0.5 and abs(dy) < 0.5
    # on a flat ground at the height of the rim the floor of the trench moves along the line of
    # sight: by depth * tan(theta), away from the satellite (east)
    dem = sat._Dem(satellites["dem"])
    with rasterio.open(out) as src:
        tr = src.transform
        x = tr.c + (np.arange(src.width) + 0.5) * tr.a
        y = tr.f + (np.arange(src.height) + 0.5) * tr.e
    X, Y = np.meshgrid(x, y)
    z = dem.sample(X, Y)
    rim = float(np.nanpercentile(z, 90))
    depth = rim - float(np.nanpercentile(z, 5))
    assert depth > 0.25
    flat = tmp_path / "flat.tif"
    with rasterio.open(satellites["dem"]) as src:
        profile = src.profile.copy()
        with rasterio.open(flat, "w", **profile) as dst:
            dst.write(np.full((1, src.height, src.width), rim, np.float32))
    out2 = tmp_path / "east_flat.tif"
    sat.orthorectify(s["path"], flat, out2, resolution=satellites["gsd"], bounds=satellites["bounds"])
    with rasterio.open(out2) as src:
        flat_grey = src.read().astype(np.float32).mean(axis=0)
        assert src.width == data.shape[2] and src.height == data.shape[1]
    k = int(round(depth * np.tan(np.radians(THETA)) / satellites["gsd"]))
    assert k >= 8
    floor = np.isfinite(z) & (z < rim - 0.2) & (flat_grey > 0) & ref_ok
    floor = cv2.erode(floor.astype(np.uint8), np.ones((2 * k + 9, 2 * k + 9), np.uint8)) > 0
    assert floor.sum() > 500
    # the flat orthorectification shows, at every cell, the ground about k cells further west
    corr = {j: np.corrcoef(flat_grey[floor], np.roll(ref, j, axis=1)[floor])[0, 1] for j in range(-k - 6, k + 7)}
    best = max(corr, key=corr.get)
    assert abs(best - k) <= 2 and corr[best] > 0.75 and corr[best] > corr[0] + 0.2


def test_stereo_pair_recovers_the_surface(satellites, tmp_path):
    import rasterio

    east, west = satellites["scenes"]["east"], satellites["scenes"]["west"]
    out = tmp_path / "dsm.tif"
    z0 = satellites["h0"]
    r = sat.stereo_dsm(east["path"], west["path"], out, crs=satellites["chunk"].crs, resolution=satellites["gsd"],
                       height_range=(z0 - 1.0, z0 + 1.0), bounds=satellites["bounds"])
    assert r["base_to_height"] == pytest.approx(2 * np.tan(np.radians(THETA)), rel=0.02)
    assert r["matched"] > 0.6
    with rasterio.open(out) as src:
        dsm = src.read(1).astype(np.float64)
        dsm[dsm == src.nodata] = np.nan
        tr = src.transform
        x = tr.c + (np.arange(src.width) + 0.5) * tr.a
        y = tr.f + (np.arange(src.height) + 0.5) * tr.e
    X, Y = np.meshgrid(x, y)
    truth = sat._Dem(satellites["dem"]).sample(X, Y)
    ok = np.isfinite(dsm) & np.isfinite(truth)
    assert ok.mean() > 0.5
    err = np.abs(dsm - truth)[ok]
    assert np.median(err) < 0.015 and np.percentile(err, 75) < 0.05
    assert np.nanpercentile(dsm, 5) == pytest.approx(np.nanpercentile(truth[ok], 5), abs=0.03)  # the floor
    assert np.nanpercentile(dsm, 90) == pytest.approx(np.nanpercentile(truth[ok], 90), abs=0.03)  # the rim


def test_scene_and_pair_in_the_project(satellites, tmp_path):
    """``ortho_scene`` keeps the 8-bit rendition as a version of the orthomosaic and the full
    result beside it; ``dsm_from_pair`` a version of the DEM; both noted in ``meta["satellite"]``."""
    import shutil

    import rasterio

    doc = ppg.Document()
    ch = doc.add_chunk("Satellite")
    doc.save(tmp_path / "sat.ppg")
    ch.crs = satellites["chunk"].crs
    dem = ch.data_dir / "dem.tif"
    shutil.copy2(satellites["dem"], dem)
    ch._set_product("dem", dem)
    east, west = satellites["scenes"]["east"], satellites["scenes"]["west"]
    r = sat.ortho_scene(ch, east["path"], name="Est", resolution=satellites["gsd"], rgb=[1, 2, 3])
    assert (ch.data_dir / "satellite" / "Est_ortho.tif").exists()
    versions = ch.meta["versions"]["orthomosaic"]
    assert len(versions) == 1 and versions[0]["name"] == "Satellite: Est" and versions[0]["id"] == r["version"]
    with rasterio.open(ch.data_dir / versions[0]["files"]["orthomosaic"]) as src:
        assert src.count == 4 and src.dtypes[0] == "uint8"
        rgba = src.read()
        assert rgba[3].max() == 255 and rgba[:3][:, rgba[3] > 0].std() > 10
    assert ch.meta["satellite"]["scenes"][0]["name"] == "Est"
    r2 = sat.dsm_from_pair(ch, east["path"], west["path"], name="Est + Ovest", resolution=satellites["gsd"],
                           bounds=satellites["bounds"])
    assert r2["base_to_height"] > 0.5 and r2["matched"] > 0.5
    dems = ch.meta["versions"]["dem"]
    assert len(dems) == 1 and dems[0]["name"].startswith("DSM stereo") and dems[0]["id"] == r2["version"]
    assert (ch.data_dir / dems[0]["files"]["dem"]).exists() and (ch.data_dir / "satellite" / "Est_Ovest_dsm.tif").exists()
    assert ch.meta["satellite"]["stereo"][0]["scenes"][0] == str(east["path"])
    doc.save()
    doc2 = ppg.Document.open(doc.path)
    assert doc2.chunks[0].meta["satellite"]["scenes"][0]["version"] == r["version"]


def test_command_line_and_the_menu(satellites, tmp_path, monkeypatch):
    """``ppg satellite info|ortho|dsm`` and the menu actions of the window (the dialogs answered
    by a stand-in) on a project with the DEM of the trench."""
    import shutil

    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    east, west = satellites["scenes"]["east"], satellites["scenes"]["west"]
    doc = ppg.Document()
    ch = doc.add_chunk("Satellite")
    doc.save(tmp_path / "sat.ppg")
    ch.crs = satellites["chunk"].crs
    shutil.copy2(satellites["dem"], ch.data_dir / "dem.tif")
    ch._set_product("dem", ch.data_dir / "dem.tif")
    doc.save()
    r = CliRunner().invoke(app, ["satellite", str(doc.path), "info", str(east["path"])])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert '"bands": 3' in r.output and '"gsd"' in r.output
    r = CliRunner().invoke(app, ["satellite", str(doc.path), "ortho", str(east["path"]), "--name", "Est",
                                 "--resolution", str(satellites["gsd"]), "--rgb", "1,2,3"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "version" in r.output and "of the orthomosaic" in r.output
    r = CliRunner().invoke(app, ["satellite", str(doc.path), "dsm", str(east["path"]), str(west["path"]),
                                 "--resolution", str(satellites["gsd"]), "--name", "Coppia"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "base to height" in r.output and "of the DEM" in r.output
    doc2 = ppg.Document.open(doc.path)
    assert len(doc2.chunks[0].meta["versions"]["orthomosaic"]) == 1 and len(doc2.chunks[0].meta["versions"]["dem"]) == 1

    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    qapp = QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    w = mwmod.MainWindow(doc2, autoconnect=False)
    w.runner.show_dialog = False
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append((title, text)))
    monkeypatch.setattr(w, "_warn", lambda text: shown.append(("warn", text)))
    answers = iter([
        {"scene": str(east["path"]), "rpc": ""},
        {"scene": str(east["path"]), "rpc": "", "name": "Est 2", "resolution": satellites["gsd"], "height_offset": 0.0,
         "rgb": "3,2,1", "resampling": "linear"},
        {"scene_a": str(east["path"]), "rpc_a": "", "scene_b": str(west["path"]), "rpc_b": "", "name": "",
         "resolution": satellites["gsd"], "zmin": None, "zmax": None, "height_offset": 0.0, "use_dem": True},
    ])
    monkeypatch.setattr(mwmod.ParamDialog, "ask", staticmethod(lambda parent, title, fields, intro="": dict(next(answers))))
    w.sat_info()
    assert shown[-1][0] == "Informazioni sulla scena" and "3 bande" in shown[-1][1]
    w.sat_ortho()
    w.runner.wait()
    qapp.processEvents()
    assert shown[-1][0] == "Ortorettifica" and "Est 2" in shown[-1][1]
    w.sat_dsm()
    w.runner.wait()
    qapp.processEvents()
    assert shown[-1][0] == "DSM da una coppia stereo" and "base/altezza" in shown[-1][1]
    assert len(w.chunk.meta["versions"]["orthomosaic"]) == 2 and len(w.chunk.meta["versions"]["dem"]) == 2
    w.setWindowModified(False)
    w.close()
