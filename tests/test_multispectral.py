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


def _p4m_capture(folder: Path, number: int, uuid: str, rng, shift=(0, 0)) -> None:
    """Six files as a DJI P4 Multispectral writes them: DJI_nnn0.JPG (RGB) and five 16-bit TIFFs
    with the band, the capture and the radiometry as XMP attributes, the black level in the EXIF."""
    from PIL import Image
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    base = rng.uniform(0.2, 0.8, (260, 320)).astype(np.float32)
    bands = {"Blue": (1, 2870), "Green": (2, 1232), "Red": (3, 2780), "RedEdge": (4, 1900), "NIR": (5, 1480)}
    Image.fromarray(np.zeros((260, 320, 3), np.uint8)).save(folder / f"DJI_{number:04d}.JPG", quality=80)
    for band, (k, exposure) in bands.items():
        dx, dy = (shift if band != "Red" else (0, 0))
        img = np.roll(base, (dy, dx), axis=(0, 1))
        dn = (4096 + img * 40000 * exposure / 2000).astype(np.uint16)
        xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" '
               f'drone-dji:BandName="{band}" drone-dji:CaptureUUID="{uuid}" drone-dji:Irradiance="3500.5" '
               f'drone-dji:SensorGain="1.000" drone-dji:ExposureTime="{exposure}" drone-dji:SensorGainAdjustment="1.5" '
               f'drone-dji:CalibratedOpticalCenterX="160.0" drone-dji:CalibratedOpticalCenterY="130.0" '
               f'drone-dji:VignettingData="0.0002, 1.0e-6, 0, 0, 0, 0"/></rdf:RDF></x:xmpmeta>').encode()
        info = ImageFileDirectory_v2()
        info[700] = xmp
        info[50714] = 4096
        Image.fromarray(dn).save(folder / f"DJI_{number + k:04d}.TIF", tiffinfo=info)


def test_dji_p4_multispectral_files(tmp_path):
    """The P4 Multispectral: bands and captures from the XMP (CaptureUUID), the numbering that
    starts again in a second folder, the black level from the EXIF, DJI's radiometric model."""
    rng = np.random.default_rng(5)
    a, b = tmp_path / "100MEDIA", tmp_path / "101MEDIA"
    a.mkdir()
    b.mkdir()
    _p4m_capture(a, 10, "aaaa1", rng)
    _p4m_capture(a, 20, "aaaa2", rng, shift=(3, -2))
    _p4m_capture(b, 10, "bbbb1", rng)
    caps = ms.detect_bands([a, b])
    assert set(caps) == {"DJI_0011", "DJI_0021", "101MEDIA_DJI_0011"}
    assert all(set(c) == {"Blue", "Green", "Red", "RedEdge", "NIR"} for c in caps.values())
    assert caps["DJI_0021"]["NIR"].name == "DJI_0025.TIF" and caps["101MEDIA_DJI_0011"]["Red"].parent == b
    tags = ms.xmp_of(caps["DJI_0011"]["Red"])
    assert tags["BandName"] == "Red" and tags["BlackLevel"] == 4096 and tags["CaptureUUID"] == "aaaa1"
    rad, used = ms.radiance_image(caps["DJI_0011"]["Red"], tags)
    assert used["model"] == "dji" and used["exposure"] == pytest.approx(0.00278) and used["vignetting"]
    assert used["black_level"] == 4096 and used["gain_adjustment"] == 1.5
    dn = ms.read_band(caps["DJI_0011"]["Red"])
    centre = (dn[130, 160] - 4096) / 65535 * 1.5 / 0.00278  # no vignetting at the optical centre
    assert rad[130, 160] == pytest.approx(centre, rel=1e-3)
    r = np.hypot(0 - 160, 0 - 130)
    corner = (dn[0, 0] - 4096) / 65535 * (1 + 0.0002 * r + 1e-6 * r * r) * 1.5 / 0.00278
    assert rad[0, 0] == pytest.approx(corner, rel=1e-3)
    refl = ms.reflectance_image(caps["DJI_0011"]["Red"], tags=tags)
    assert refl[130, 160] == pytest.approx(centre / 3500.5, rel=1e-3)
    # the bands of the second capture are shifted: the chain registration finds the shift
    imgs = {band: ms.read_band(p) for band, p in caps["DJI_0021"].items()}
    quality: dict = {}
    hs = ms.register_bands(imgs, "Red", quality=quality)
    for band, h in hs.items():
        if band != "Red":
            assert abs(h[0, 2] - 3) < 0.5 and abs(h[1, 2] + 2) < 0.5, (band, h)
            assert quality[band]["correlation"] > 0.5
    assert quality["NIR"]["on"] in ("RedEdge", "Red")


def test_pane_tree_and_views_show_the_products(scene_dir, tmp_path, monkeypatch):
    """The products in the project's tree, the pane's state and the raster view: a reflectance
    orthomosaic in false colour, an index on its ramp, the zones by class with their polygons as a
    layer - on small files written here, without the whole chain."""
    pytest.importorskip("PySide6")
    import os

    import rasterio
    from rasterio.transform import from_origin

    from pyarchinit_photogrammetry.io import gpkg

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    qapp = QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import image_views
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    ch.crs = "EPSG:3004"
    folder = ch.data_dir / "multispectral"
    folder.mkdir(parents=True)
    h, w = 60, 80
    tr = from_origin(2480000.0, 4500000.0, 0.05, 0.05)
    rng = np.random.default_rng(2)
    bands = ["Blue", "Green", "Red", "RedEdge", "NIR"]
    data = rng.uniform(0.01, 0.1, (5, h, w)).astype(np.float32)
    data[4, :, :40] += 0.4  # vegetation on the left: NIR high
    with rasterio.open(folder / "reflectance.tif", "w", driver="GTiff", height=h, width=w, count=5, dtype="float32",
                       crs="EPSG:3004", transform=tr, nodata=float("nan")) as dst:
        dst.write(data)
        for i, b in enumerate(bands):
            dst.set_band_description(i + 1, b)
    ch.meta["multispectral"] = {"reference": "Red", "bands": bands, "captures": 12, "panel": None, "registration": {},
                                "reflectance": "multispectral/reflectance.tif", "reflectance_bands": bands}
    ndvi = ms.index_raster(ch, "NDVI")
    summary = ms.export_zoning(ch, "NDVI", tmp_path / "zone.gpkg", classes=2, method="kmeans", min_cells=5)
    assert summary["polygons"] >= 2 and (ch.data_dir / summary["raster"]).exists()
    doc.save()
    w_ = mwmod.MainWindow(doc, autoconnect=False)
    w_.runner.show_dialog = False
    try:
        # the tree: the multispectral node with its three products
        tree = w_.workspace
        chunk_item = tree.topLevelItem(0).child(0)
        nodes = [chunk_item.child(i) for i in range(chunk_item.childCount())]
        node = next(n for n in nodes if n.data(0, mwmod.Qt.ItemDataRole.UserRole)[0] == "multispectral")
        payloads = [node.child(i).data(0, mwmod.Qt.ItemDataRole.UserRole)[2] for i in range(node.childCount())]
        assert payloads == ["ms:reflectance", "ms:index:NDVI", "ms:zones:NDVI"]
        # the pane says what there is
        assert "12" in w_.ms_pane.states["captures"].text() and "NDVI" in w_.ms_pane.states["index"].text()
        assert w_.ms_pane.b_index.isEnabled() and w_.ms_pane.b_zoning.isEnabled() and w_.ms_pane.b_align.isEnabled()
        assert not w_.ms_pane.b_surface.isEnabled()  # nothing aligned: the surface step waits
        # the views: false colour, the index, the zones (with the polygons as a layer)
        w_._workspace_item("product", ch, "ms:reflectance")
        assert w_.ortho_view.kind == "reflectance" and w_.ortho_view._bands == (5, 3, 2)
        rgba = image_views.read_window_rgba(folder / "reflectance.tif", "reflectance", (0, 0, w, h), (h, w), bands=(5, 3, 2),
                                            limits=w_.ortho_view._band_limits)
        assert rgba[..., 3].min() == 255 and rgba[:, :40, 0].mean() > rgba[:, 40:, 0].mean() + 60  # NIR shown as red
        w_._workspace_item("product", ch, "ms:index:NDVI")
        assert w_.ortho_view.kind == "index" and w_.ortho_view._dem_range[0] < w_.ortho_view._dem_range[1]
        rgba = image_views.read_window_rgba(ndvi, "index", (0, 0, w, h), (h, w), w_.ortho_view._dem_range)
        assert rgba[:, :40, 0].mean() < rgba[:, 40:, 0].mean() - 60  # red where low, dark green where high
        w_._workspace_item("product", ch, "ms:zones:NDVI")
        assert w_.ortho_view.kind == "zones" and "Zone NDVI" in w_.ortho_view.layers
        assert len(w_.ortho_view.features("Zone NDVI")) == summary["polygons"]
        rgba = image_views.read_window_rgba(ch.data_dir / summary["raster"], "zones", (0, 0, w, h), (h, w))
        assert rgba[..., 3].max() == 255 and len(np.unique(rgba[rgba[..., 3] > 0][:, :3], axis=0)) == 2
        rows = gpkg.read_table(tmp_path / "zone.gpkg", "zone")
        assert {r[1]["classe"] for r in rows} == {1, 2}
    finally:
        w_.setWindowModified(False)
        w_.close()


def test_an_index_without_its_band_is_refused_with_its_name():
    """Thermal needs LWIR: without it the error names the band (a crash in 1.2.1)."""
    bands = {"Red": np.ones((2, 2), np.float32), "NIR": np.ones((2, 2), np.float32)}
    assert ms.available_indices(bands) == [n for n in ms.INDICES if n not in ("NDRE", "GNDVI", "NDWI", "CIRE", "Thermal")]
    assert ms.missing_bands(ms.INDICES["Thermal"][0], bands) == ["LWIR"]
    with pytest.raises(ValueError, match="LWIR"):
        ms.index_values("LWIR", bands)
    with pytest.raises(ValueError, match="formula"):
        ms.index_values("NIR +", bands)
    assert ms.index_values("(NIR - Red) / (NIR + Red)", bands).shape == (2, 2)
