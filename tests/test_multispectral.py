"""Multispectral captures: bands found by their suffixes and XMP, the radiometric model of the
tags, the registration of the bands, the reflectance orthomosaic on the geometry of the reference
band, the indices and the zoning polygons - on the synthetic trench rendered as five bands."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import multispectral as ms

BANDS = {"1": "Blue", "2": "Green", "3": "Red", "4": "NIR", "5": "RedEdge"}


def _write_captures(scene_dir: Path, out: Path, n: int | None = None) -> Path:
    """Five 16-bit bands from every rendered photo: the visible bands from its grey levels, the
    NIR high where the texture is dark (a "vegetation" class the indices must find)."""
    folder = out / "ms"
    folder.mkdir(parents=True, exist_ok=True)
    photos = sorted((scene_dir / "photos").glob("*.jpg"))[:n]
    for p in photos:
        grey = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.0
        veg = grey < 0.45
        bands = {"Blue": 0.9 * grey, "Green": grey, "Red": 0.95 * grey + 0.02,
                 "NIR": np.where(veg, 0.55 + 0.3 * grey, 0.2 + 0.4 * grey), "RedEdge": np.where(veg, 0.4 + 0.3 * grey, 0.25 + 0.4 * grey)}
        for suffix, name in BANDS.items():
            dn = np.clip(bands[name] * 40000 + 500, 0, 65535).astype(np.uint16)
            cv2.imwrite(str(folder / f"{p.stem}_{suffix}.tif"), dn)
    return folder


def test_bands_found_and_the_radiometry_of_the_tags(scene_dir, tmp_path):
    folder = _write_captures(scene_dir, tmp_path, n=3)
    captures = ms.detect_bands([folder])
    assert len(captures) == 3 and all(set(c) == set(BANDS.values()) for c in captures.values())
    stem = next(iter(captures))
    assert captures[stem]["Red"].name.endswith("_3.tif")
    # DJI and Sequoia suffixes, an RGB JPEG left out, an XMP band name
    (tmp_path / "DJI_0007_MS_NIR.TIF").write_bytes(b"")
    (tmp_path / "DJI_0007_MS_G.TIF").write_bytes(b"")
    (tmp_path / "DJI_0007_D.JPG").write_bytes(b"")
    (tmp_path / "IMG_0003_GRE.TIF").write_bytes(b"")
    (tmp_path / "IMG_0003_RED.TIF").write_bytes(b"")
    found = ms.detect_bands([tmp_path / "DJI_0007_MS_NIR.TIF", tmp_path / "DJI_0007_MS_G.TIF", tmp_path / "DJI_0007_D.JPG",
                             tmp_path / "IMG_0003_GRE.TIF", tmp_path / "IMG_0003_RED.TIF"])
    assert set(found["DJI_0007"]) == {"NIR", "Green"} and set(found["IMG_0003"]) == {"Green", "Red"}
    xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="r"><rdf:Description xmlns:Camera="c" '
           'Camera:BandName="Red edge" Camera:BlackLevel="4800" Camera:ExposureTime="1/400" Camera:SensorGain="2"'
           '><Camera:RadiometricCalibration><rdf:Seq><rdf:li>0.00014</rdf:li><rdf:li>3.0e-07</rdf:li>'
           '<rdf:li>1.0e-05</rdf:li></rdf:Seq></Camera:RadiometricCalibration>'
           '<Camera:VignettingCenter><rdf:Seq><rdf:li>640</rdf:li><rdf:li>480</rdf:li></rdf:Seq></Camera:VignettingCenter>'
           '<Camera:Irradiance>1.25</Camera:Irradiance></rdf:Description></rdf:RDF></x:xmpmeta>')
    tags = ms.parse_xmp(xmp)
    assert tags["BandName"] == "Red edge" and tags["RadiometricCalibration"] == ["0.00014", "3.0e-07", "1.0e-05"]
    assert ms._number(tags["ExposureTime"]) == 0.0025 and ms.irradiance_of(tags) == 1.25
    rad, used = ms.radiance_image(captures[stem]["Red"], tags)
    assert used["model"] == "micasense" and used["black_level"] == 4800 and rad.dtype == np.float32
    assert np.isfinite(rad).all() and rad.max() > 0
    plain, used2 = ms.radiance_image(captures[stem]["Red"], {})
    assert used2["model"] == "normalised" and 0 < plain.max() <= 1.0
    refl = ms.reflectance_image(captures[stem]["Red"], None, tags)
    assert np.allclose(refl, np.pi * rad / 1.25)
    assert np.allclose(ms.reflectance_image(captures[stem]["Red"], 2.0, {}), 2.0 * plain)


def test_registration_finds_a_shift(scene_dir):
    photo = sorted((scene_dir / "photos").glob("*.jpg"))[0]
    base = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.0
    base = cv2.resize(base, (600, 450), interpolation=cv2.INTER_AREA)
    shift = np.array([[1.0, 0.0, 4.0], [0.0, 1.0, -3.0]], np.float32)  # the band 4 px right, 3 up
    shifted = cv2.warpAffine(base, shift, (base.shape[1], base.shape[0]), flags=cv2.INTER_LINEAR)
    hs = ms.register_bands({"Red": base, "NIR": shifted}, "Red")
    assert np.allclose(hs["Red"], np.eye(3))
    assert abs(hs["NIR"][0, 2] - 4.0) < 0.5 and abs(hs["NIR"][1, 2] + 3.0) < 0.5
    back = ms.warp_band(shifted, hs["NIR"], base.shape)
    inner = (slice(20, -20), slice(20, -20))
    assert np.nanmean(np.abs(back[inner] - base[inner])) < 0.2 * np.nanmean(np.abs(shifted[inner] - base[inner]))


def test_reflectance_orthomosaic_indices_and_zoning(scene_dir, tmp_path):
    folder = _write_captures(scene_dir, tmp_path)
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    cams = ms.add_captures(ch, [folder])
    assert len(cams) == len(list((scene_dir / "photos").glob("*.jpg"))) and ch.meta["multispectral"]["reference"] == "Red"
    assert all(set(c.meta["bands"]) == set(BANDS.values()) for c in cams)
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", gpu="off")
    assert ch.align_cameras(mapper="incremental", use_gps=False) >= 0.9 * len(cams)
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.003
    # a panel: a flat patch of the first capture with a known reflectance
    factors = ms.calibrate_panel(ch, cams[0], (10, 10, 60, 60), 0.5)
    assert set(factors) == set(BANDS.values()) and all(f > 0 for f in factors.values())
    ch.build_depth_maps(quality="medium")  # the default backend: SGBM without the native core
    ch.build_point_cloud()
    ch.build_dem()
    out = ms.build_reflectance(ch, resolution=0.01)
    import rasterio

    with rasterio.open(out) as src:
        assert src.count == 5 and list(src.descriptions) == ch.meta["multispectral"]["bands"] and src.dtypes[0] == "float32"
        nir = src.read(src.descriptions.index("NIR") + 1)
        red = src.read(src.descriptions.index("Red") + 1)
    valid = np.isfinite(nir) & np.isfinite(red)
    assert valid.mean() > 0.3
    ndvi_path = ms.index_raster(ch, "NDVI")
    with rasterio.open(ndvi_path) as src:
        ndvi = src.read(1)
    ok = np.isfinite(ndvi)
    assert ok.sum() == valid.sum() and ndvi[ok].max() > 0.3 and ndvi[ok].min() < 0.15  # the two classes of the texture
    summary = ms.export_zoning(ch, "NDVI", tmp_path / "zone.gpkg", classes=2, method="kmeans", min_cells=50)
    assert summary["polygons"] >= 2 and len(summary["breaks"]) == 1 and 0.1 < summary["breaks"][0] < 0.4
    from pyarchinit_photogrammetry.io import gpkg

    rows = gpkg.read_table(tmp_path / "zone.gpkg", "zone")
    assert len(rows) == summary["polygons"] and {r[1]["classe"] for r in rows} == {1, 2}
    assert sum(summary["area_m2"].values()) > 0.5 * (valid.sum() * 0.01 * 0.01)
    cls, breaks = ms.zoning(ndvi, classes=3, method="quantiles", min_cells=1)
    assert set(np.unique(cls[ok])) == {1, 2, 3} and len(breaks) == 2
    with pytest.raises(ValueError):
        ms.zoning(ndvi, method="somehow")
    assert ms.index_values("NIR / Red", {"NIR": np.array([2.0]), "Red": np.array([0.0])}).tolist() != [1.0]


def test_command_line_and_the_menu(scene_dir, tmp_path, monkeypatch):
    """`ppg multispectral add|bands|panel` on a fresh project, then the menu actions of the window
    (the dialogs answered by a stand-in): captures added and the panel's factors reported."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    folder = _write_captures(scene_dir, tmp_path, n=4)
    doc = ppg.Document()
    doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    r = CliRunner().invoke(app, ["multispectral", str(doc.path), "add", str(folder)])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "4 captures" in r.output and "reference Red" in r.output
    r = CliRunner().invoke(app, ["multispectral", str(doc.path), "bands"])
    assert r.exit_code == 0 and '"reference": "Red"' in r.output
    first = sorted(folder.glob("*_3.tif"))[0]
    r = CliRunner().invoke(app, ["multispectral", str(doc.path), "panel", str(first), "--region", "10,10,60,60"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "panel factors" in r.output and "NIR" in r.output
    r = CliRunner().invoke(app, ["multispectral", str(doc.path), "index"])
    assert r.exit_code != 0  # no reflectance orthomosaic yet

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

    doc2 = ppg.Document()
    doc2.add_chunk("Campo")
    doc2.save(tmp_path / "campo2.ppg")
    w = mwmod.MainWindow(doc2, autoconnect=False)
    w.runner.show_dialog = False
    answers = iter([{"folder": str(folder), "reference": "auto"},
                    {"camera": None, "x0": 10, "y0": 10, "x1": 60, "y1": 60, "reflectance": 0.5}])
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append((title, text)))
    monkeypatch.setattr(w, "_warn", lambda text: shown.append(("warn", text)))

    def ask(parent, title, fields, intro=""):
        v = dict(next(answers))
        if v.get("camera") is None and "camera" in v:
            v["camera"] = fields[0].default
        return v

    monkeypatch.setattr(mwmod.ParamDialog, "ask", staticmethod(ask))
    w.ms_add()
    w.runner.wait()
    qapp.processEvents()
    assert shown and shown[-1][0] == "Riprese multispettrali" and "4 riprese" in shown[-1][1]
    assert len(ms.multispectral_cameras(w.chunk)) == 4
    w.ms_panel()
    qapp.processEvents()
    assert shown[-1][0] == "Pannello di calibrazione" and "NIR" in shown[-1][1]
    w.ms_index()
    assert shown[-1][0] == "warn"  # the reflectance orthomosaic comes first
    w.setWindowModified(False)
    w.close()
