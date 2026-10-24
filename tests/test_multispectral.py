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


def _write_captures(scene_dir: Path, out: Path, n: int | None = None, odd_gain: float = 1.0, odd_slope: float = 0.0,
                    radial: float = 0.0) -> Path:
    """Five 16-bit bands from every rendered photo: the visible bands from its grey levels, the
    NIR high where the texture is dark (a "vegetation" class the indices must find). ``odd_gain``
    multiplies every band of every other photo: the strips between flight lines; ``odd_slope``
    makes their light change across the frame (exp(slope u), u from -0.5 to 0.5 left to right: a
    cloud's shadow, the reflectance seen from the side), ``radial`` brightens the edges of every
    frame (exp(radial (u2 + v2 - 1/6)), of mean one)."""
    folder = out / "ms"
    folder.mkdir(parents=True, exist_ok=True)
    photos = sorted((scene_dir / "photos").glob("*.jpg"))[:n]
    for i, p in enumerate(photos):
        grey = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.0
        veg = grey < 0.45
        bands = {"Blue": 0.9 * grey, "Green": grey, "Red": 0.95 * grey + 0.02,
                 "NIR": np.where(veg, 0.55 + 0.3 * grey, 0.2 + 0.4 * grey), "RedEdge": np.where(veg, 0.4 + 0.3 * grey, 0.25 + 0.4 * grey)}
        h, w = grey.shape
        u = (np.arange(w, dtype=np.float32) + 0.5)[None, :] / w - 0.5
        v = (np.arange(h, dtype=np.float32) + 0.5)[:, None] / h - 0.5
        field = np.exp(radial * (u * u + v * v - 1.0 / 6.0) + (odd_slope * u if i % 2 else 0.0))
        gain = odd_gain if i % 2 else 1.0
        for suffix, name in BANDS.items():
            dn = np.clip(bands[name] * gain * field * 40000 + 500, 0, 65535).astype(np.uint16)
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


def test_balance_finds_the_gains_the_planes_and_the_radial_term_of_the_photos():
    """The fit of the balance on made-up readings: 60 photos, four of them 2.5 times brighter or
    darker (the outlier test looked at the readings with the photo's gain still in them and threw
    those photos' readings away), each with a plane across its frame, all brighter at the edges,
    and a few readings far off (a moving shadow): everything found within a few hundredths; a gain
    per photo alone leaves the readings of a point disagreeing more."""
    rng = np.random.default_rng(1)
    n_photos, n_points = 60, 3000
    g = rng.normal(0, 0.2, n_photos)
    g[:4] = [0.9, -0.9, 0.8, -0.85]
    a, b, c = rng.normal(0, 0.25, n_photos), rng.normal(0, 0.25, n_photos), 0.5
    value = rng.normal(-2.5, 0.6, n_points)
    point = np.repeat(np.arange(n_points), 8)  # every point read by 8 photos at places of their frames
    photo = rng.integers(0, n_photos, len(point))
    u, v = rng.uniform(-0.5, 0.5, len(point)), rng.uniform(-0.5, 0.5, len(point))
    logv = value[point] + g[photo] + a[photo] * u + b[photo] * v + c * (u * u + v * v - 1 / 6)
    logv += rng.normal(0, 0.05, len(point))
    far = rng.random(len(point)) < 0.02
    logv[far] += rng.normal(0, 1.0, far.sum())
    coef, radial, n_ph, residual = ms._fit_balance(point, photo, logv, u, v, n_points, n_photos)
    assert (n_ph > 0).all()
    assert np.max(np.abs(coef[:, 0] - (g - g.mean()))) < 0.03
    assert np.max(np.abs(coef[:, 1] - a)) < 0.06 and np.max(np.abs(coef[:, 2] - b)) < 0.06
    assert radial == pytest.approx(c, abs=0.03) and residual < 0.08
    alone, _radial, _n, residual_alone = ms._fit_balance(point, photo, logv, u, v, n_points, n_photos, planes=False)
    assert np.max(np.abs(alone[:, 0] - (g - g.mean()))) < 0.06 and residual_alone > 2 * residual
    # what the photos are divided by: the plane and the radial term on the pixels of the frame
    gains = {"NIR": {"7": [float(np.exp(0.2)), 0.3, -0.1]}, "radial": {"NIR": 0.5}}
    f = ms._balance_field(gains, "NIR", "7", (4, 8))
    uu = (np.arange(8) + 0.5) / 8 - 0.5
    vv = (np.arange(4) + 0.5) / 4 - 0.5
    want = np.exp(0.2 + 0.3 * uu[None, :] - 0.1 * vv[:, None] + 0.5 * (uu[None, :] ** 2 + vv[:, None] ** 2 - 1 / 6))
    assert np.allclose(f, want, rtol=1e-5)
    assert ms._balance_field({"NIR": {"7": 1.25}}, "NIR", "7", (4, 8)) == pytest.approx(1.25)  # a gain alone (before 1.3)
    assert ms._balance_field(gains, "Red", "7", (4, 8)) is None
    # the plane holds where the photo's points were: beyond them it stays at its edge (a photo at
    # the edge of the block, its points on one side only: prolonged, the plane whitened the rest)
    edge = {"NIR": {"7": [1.0, 2.0, 0.0, -0.5, 0.0, -0.5, 0.5]}}
    f = ms._balance_field(edge, "NIR", "7", (2, 8))
    assert np.allclose(f[0, :4], np.exp(2.0 * uu[:4])) and np.allclose(f[0, 4:], 1.0)
    side = (np.repeat(np.arange(20), 30), np.zeros(600, int))  # one photo read on the left of its frame only
    uu2 = rng.uniform(-0.5, -0.1, 600)
    found = ms._photo_ranges(side[1], uu2, rng.uniform(-0.5, 0.5, 600), 1)
    assert found[0, 0] == pytest.approx(-0.49, abs=0.02) and found[0, 1] == pytest.approx(-0.11, abs=0.02)


def test_balance_is_not_tilted_by_a_shadow_that_moved():
    """Ten photos of a second flight see a fifth of the points three times darker (the shadow of a
    tree that moved in the hour between the flights), all on the left of their frames: weighed as
    the others, those readings tilted the planes of the ten photos (x2.8 across the frame on a
    real survey, the shadow whitened in the orthomosaic); weighed by their residuals they do not."""
    rng = np.random.default_rng(4)
    n_photos, n_points = 40, 3000
    value = rng.normal(-2.5, 0.5, n_points)
    point = np.repeat(np.arange(n_points), 8)
    photo = rng.integers(0, n_photos, len(point))
    u, v = rng.uniform(-0.5, 0.5, len(point)), rng.uniform(-0.5, 0.5, len(point))
    logv = value[point] + rng.normal(0, 0.05, len(point))
    shadow = (photo >= 30) & (point < n_points // 5) & (u < 0)
    logv[shadow] -= np.log(3.0)
    coef, _radial, _n, _residual = ms._fit_balance(point, photo, logv, u, v, n_points, n_photos)
    assert np.max(np.abs(coef[30:, 1])) < 0.1 and np.max(np.abs(coef[:, 0])) < 0.05


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
    """The chain on captures where every other photo is 30 % brighter (the strips between flight
    lines), its light changing across the frame, and every frame brighter at its edges: the
    balance of the photos finds the gain, the plane of every photo and the radial term, and the
    reflectance orthomosaic is seamless."""
    folder = _write_captures(scene_dir, tmp_path, odd_gain=1.3, odd_slope=0.3, radial=0.4)
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
    # the balance: the odd photos were written 30 % brighter in every band, their light rising by
    # 0.3 (log) from the left of the frame to the right, every frame 0.4 brighter at the edges
    gains = ms.balance_gains(ch, ["NIR", "Red"])
    odd = {c.meta["capture"] for i, c in enumerate(cams) if i % 2}
    for band in ("NIR", "Red"):
        g = gains[band]
        hi = np.array([g[str(c.key)] for c in cams if c.meta["capture"] in odd and str(c.key) in g])
        lo = np.array([g[str(c.key)] for c in cams if c.meta["capture"] not in odd and str(c.key) in g])
        assert len(hi) > 3 and len(lo) > 3
        assert np.median(hi[:, 0]) / np.median(lo[:, 0]) == pytest.approx(1.3, rel=0.05), band
        assert gains["spread"][band] == pytest.approx(np.log(1.3) / 2, rel=0.2)
        assert np.median(hi[:, 1]) == pytest.approx(0.3, abs=0.06) and abs(np.median(lo[:, 1])) < 0.06, band
        assert np.abs(np.median(np.vstack([hi, lo])[:, 2])) < 0.06
        assert gains["radial"][band] == pytest.approx(0.4, abs=0.08), band
    # a gain per photo alone leaves the light changing inside the frames: the readings of a point
    # disagree more between the photos
    alone = ms.balance_gains(ch, ["NIR"], planes=False)
    assert gains["residual"]["NIR"] < 0.7 * alone["residual"]["NIR"]
    out = ms.build_reflectance(ch, resolution=0.01)
    import rasterio

    assert set(ch.meta["multispectral"]["balance"]) >= {"NIR", "Red", "spread"}
    with rasterio.open(out) as src:
        assert src.count == 5 and list(src.descriptions) == ch.meta["multispectral"]["bands"] and src.dtypes[0] == "float32"
        nir = src.read(src.descriptions.index("NIR") + 1)
        red = src.read(src.descriptions.index("Red") + 1)
    valid = np.isfinite(nir) & np.isfinite(red)
    assert valid.mean() > 0.3
    # without the balance the mosaic is a chequerboard of the photos' gains (odd x 1.3): against
    # the balanced one (gains of mean one: odd / 1.14, even x 1.14) the cells split in two groups
    # at +-0.13 in log, and the means of 40 x 40 blocks vary more
    raw = ms.build_reflectance(ch, resolution=0.01, balance=False)
    assert "balance" not in ch.meta["multispectral"]
    with rasterio.open(raw) as src:
        red_raw = src.read(src.descriptions.index("Red") + 1)
    both = np.isfinite(red) & np.isfinite(red_raw) & (red > 0.02) & (red_raw > 0.02)
    lr = np.log(red_raw[both] / red[both])
    assert np.percentile(lr, 95) - np.percentile(lr, 5) > 0.2 and abs(np.median(lr)) < 0.05

    def block_cv(a):
        means = [a[r:r + 40, c:c + 40][np.isfinite(a[r:r + 40, c:c + 40])].mean()
                 for r in range(0, a.shape[0] - 40, 40) for c in range(0, a.shape[1] - 40, 40)
                 if np.isfinite(a[r:r + 40, c:c + 40]).sum() > 800]
        return float(np.std(means) / np.mean(means))

    assert block_cv(red) < block_cv(red_raw)
    out = ms.build_reflectance(ch, resolution=0.01)  # the balanced one again for the rest
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
    assert set(ms.detect_bands([tmp_path], recursive=True)) == set(caps) and not ms.detect_bands([tmp_path])
    sniff = ms.looks_multispectral([tmp_path], recursive=True)
    assert sniff["captures"] == 3 and sniff["bands"] == ["Blue", "Green", "NIR", "Red", "RedEdge"]
    assert ms.looks_multispectral([tmp_path]) is None  # nothing in the folder itself
    # the rolls added one after the other (the second says "0 photos added" in 1.2.2): the names
    # that repeat take the folder, the reference band stays, the same files are not added twice
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    assert len(ms.add_captures(ch, [a])) == 2 and ch.meta["multispectral"]["reference"] == "Red"
    assert len(ms.add_captures(ch, [b], reference="NIR")) == 1
    info = ch.meta["multispectral"]
    assert info["captures"] == 3 and info["reference"] == "Red"
    assert {c.meta["capture"] for c in ms.multispectral_cameras(ch)} == {"DJI_0011", "DJI_0021", "101MEDIA_DJI_0011"}
    assert ms.add_captures(ch, [a, b]) == [] and ch.meta["multispectral"]["captures"] == 3
    assert len({Path(c.path).name for c in ch.cameras}) == 3
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


def _micasense_band(path: Path, dn: np.ndarray, band: str, black: int = 4800, exposure: float = 0.0005,
                    iso: int = 200, xmp: str = "", model: str = "RedEdge-M", software: str = "v7.5.0") -> None:
    """A band as a MicaSense camera writes it: a TIFF whose XMP (tag 700) carries the band and the
    radiometric calibration as elements, the black level and the bits in the first directory, the
    exposure, the ISO and the focal-plane resolution in the EXIF directory (nowhere else)."""
    from PIL import Image
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    packet = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
              '<rdf:Description rdf:about="" xmlns:Camera="http://pix4d.com/camera/1.0/" '
              'xmlns:MicaSense="http://micasense.com/MicaSense/1.0/" xmlns:DLS="http://micasense.com/DLS/1.0/">'
              f'<Camera:BandName>{band}</Camera:BandName>{xmp}</rdf:Description></rdf:RDF></x:xmpmeta>')
    info = ImageFileDirectory_v2()
    info[700] = packet.encode()
    info[50714] = black
    info[271] = "MicaSense"
    info[272] = model
    info[305] = software
    info[34665] = {33434: exposure, 34867: iso, 41486: 266.666667, 41487: 266.666667}
    Image.fromarray(dn.astype(np.uint16)).save(path, tiffinfo=info)


def _seq(*values) -> str:
    return "<rdf:Seq>" + "".join(f"<rdf:li>{v}</rdf:li>" for v in values) + "</rdf:Seq>"


def test_micasense_radiometry_panel_and_bands(tmp_path):
    """MicaSense's tag layout (as on the RedEdge-MX, RedEdge-MX Dual, RedEdge-P, Altum and
    Altum-PT samples of MicaSense's own repository): the bands named by the XMP whatever their
    suffix, the exposure and the gain from the EXIF directory, MicaSense's radiance model with the
    radial or the 2D vignetting polynomial, the DLS 2 irradiance in hundredths, the panel the
    camera found (ReflectArea, Albedo), a 16-bit thermal band in centikelvin."""
    rng = np.random.default_rng(3)
    h, w = 120, 160
    folder = tmp_path / "SET"
    folder.mkdir()
    cal = f"<Camera:RadiometricCalibration>{_seq('9.6e-05', '8.7e-08', '3.3e-06')}</Camera:RadiometricCalibration>"
    dls = ("<DLS:HorizontalIrradiance>77.81</DLS:HorizontalIrradiance><DLS:DirectIrradiance>104.4</DLS:DirectIrradiance>"
           "<DLS:ScatteredIrradiance>3.3</DLS:ScatteredIrradiance><DLS:SolarElevation>0.79</DLS:SolarElevation>"
           "<DLS:SpectralIrradiance>88.2</DLS:SpectralIrradiance>")
    camera = ("<Camera:PerspectiveFocalLength>5.4</Camera:PerspectiveFocalLength>"
              "<Camera:PerspectiveFocalLengthUnits>mm</Camera:PerspectiveFocalLengthUnits>"
              f"<Camera:PrincipalPoint>{w / 2 / 266.666667:.6f},{h / 2 / 266.666667:.6f}</Camera:PrincipalPoint>"
              f"<Camera:RigRelatives>0,0,0</Camera:RigRelatives>")
    vignette = f"<Camera:VignettingCenter>{_seq(78.5, 61.0)}</Camera:VignettingCenter>" \
               f"<Camera:VignettingPolynomial>{_seq('1.0e-06', '5.6e-07', '-6.0e-09')}</Camera:VignettingPolynomial>"
    panel = ("<Camera:CalibrationPicture>2</Camera:CalibrationPicture><Camera:Albedo>0.4884</Camera:Albedo>"
             f"<Camera:ReflectArea>{_seq('100,40,100,70,60,70,60,40')}</Camera:ReflectArea>")
    base = rng.uniform(0.0, 1.0, (h, w))
    dn = (6000 + 20000 * base).astype(np.uint16)
    dn[40:70, 60:100] = 30000  # the panel's reflective area
    names = {"1": "Blue", "2": "Green", "3": "Red", "4": "NIR", "5": "Red edge", "6": "Blue-444", "10": "Red edge-740"}
    for k, name in names.items():
        tags = cal + vignette + dls + camera + (panel if k != "2" else "")  # the camera missed the panel in green
        _micasense_band(folder / f"IMG_0001_{k}.tif", dn, name, xmp=tags)
    thermal = (29000 + 300 * base[::2, ::2]).astype(np.uint16)  # centikelvin
    _micasense_band(folder / "IMG_0001_7.tif", thermal, "LWIR", black=0, xmp=f"<Camera:RadiometricCalibration>{_seq(0, 0, 0)}"
                    "</Camera:RadiometricCalibration><Camera:ThermalCalibrationParameters>1,316.9</Camera:ThermalCalibrationParameters>")
    caps = ms.detect_bands([folder])
    assert set(caps) == {"IMG_0001"}
    assert set(caps["IMG_0001"]) == {"Blue", "Green", "Red", "NIR", "RedEdge", "Blue444", "RedEdge740", "LWIR"}
    assert caps["IMG_0001"]["Blue444"].name == "IMG_0001_6.tif"  # _6 is not the thermal band here
    red = caps["IMG_0001"]["Red"]
    tags = ms.xmp_of(red)
    assert tags["ExposureTime"] == pytest.approx(0.0005) and tags["ISOSpeed"] == 200
    rad, used = ms.radiance_image(red, tags)
    assert used["model"] == "micasense" and used["gain"] == 2.0 and used["exposure"] == pytest.approx(0.0005)
    a1, a2, a3 = 9.6e-05, 8.7e-08, 3.3e-06
    for y, x in ((61, 78), (0, 0), (119, 159), (30, 120)):
        r = np.hypot(x - 78.5, y - 61.0)
        fall = 1 + 1.0e-06 * r + 5.6e-07 * r ** 2 - 6.0e-09 * r ** 3
        want = (float(dn[y, x]) - 4800) / 65536 / fall * a1 / 2.0 / (0.0005 + a2 * y - a3 * 0.0005 * y)
        assert rad[y, x] == pytest.approx(want, rel=1e-4), (y, x)
    # the DLS 2 writes hundredths of W/m2/nm; a firmware that computed the horizontal value wrong
    assert ms.irradiance_of(tags) == pytest.approx(0.7781)
    assert ms.irradiance_of({**tags, "IrradianceScaleToSIUnits": "1"}) == pytest.approx(77.81)
    old = {**tags, "Software": "v4.0.0"}
    assert ms.irradiance_of(old) == pytest.approx((104.4 * np.sin(0.79) + 3.3) / 100)
    assert ms.irradiance_of({"Irradiance": "1.25"}) == 1.25  # a DLS 1: as written
    refl = ms.reflectance_image(red, None, tags)
    assert np.allclose(refl, np.pi * rad / 0.7781, rtol=1e-5)
    # the panel the camera found: its area and its reflectance from the tags; the green band, where
    # the tags have none, takes the area of another band and the mean reflectance
    found: dict = {}
    factors = ms.panel_factors(caps, "IMG_0001", found=found)
    assert set(factors) == {"Blue", "Green", "Red", "NIR", "RedEdge", "Blue444", "RedEdge740"}
    poly = np.zeros((h, w), np.uint8)
    cv2.fillPoly(poly, [np.array([[100, 40], [100, 70], [60, 70], [60, 40]], np.int32)], 1)
    mask = poly.astype(bool)
    assert factors["Red"] == pytest.approx(0.4884 / float(rad[mask].mean()), rel=1e-6)
    assert found["Green"]["from"] == "carried" and found["Green"]["reflectance"] == pytest.approx(0.4884)
    assert np.allclose(np.asarray(found["Green"]["region"]), np.asarray(found["Red"]["region"]), atol=0.5)
    # the thermal band: degrees Celsius
    lw, used_lw = ms.radiance_image(caps["IMG_0001"]["LWIR"])
    assert used_lw["model"] == "micasense_lwir" and lw[0, 0] == pytest.approx(thermal[0, 0] / 100 - 273.15, abs=1e-3)
    from pyarchinit_photogrammetry.engine import thermal as th

    assert th.kind_of(caps["IMG_0001"]["LWIR"]) == "micasense"
    # the chunk: the panel calibrated from the tags without a region
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    cams = ms.add_captures(ch, [folder])
    assert ch.meta["multispectral"]["reference"] == "Red" and ms.panel_captures(ch) == cams
    got = ms.calibrate_panel(ch, cams[0])
    assert got == pytest.approx(factors) and ch.meta["multispectral"]["panel"]["source"] == "tags"


def test_micasense_12_bit_bands_and_their_2d_vignetting(tmp_path):
    """The RedEdge-P and the Altum-PT write 12 bits (BitsPerSample 12, black level about 240): the
    normalisation is 2^12, as MicaSense's model says (MicaSense's library reads these files with
    OpenCV, which scales them to 16 bits while the black level stays in 12: its radiance came out
    16 times too high and the panel's irradiance at 17 W/m2/nm); their vignetting is a polynomial
    in x / width and y / height."""
    from PIL import Image
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    h, w = 60, 80
    dn = np.full((h, w), 2000, np.uint16)
    coef = [1.0, 0.2, -0.1, 0.3]
    powers = [0, 0, 1, 0, 0, 1, 2, 2]  # 1 + 0.2 x - 0.1 y + 0.3 x2 y2
    packet = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="r"><rdf:Description xmlns:Camera="c">'
              '<Camera:BandName>Panchro</Camera:BandName>'
              f'<Camera:RadiometricCalibration>{_seq("1.9e-04", "2.7e-09", "1.6e-05")}</Camera:RadiometricCalibration>'
              f'<Camera:VignettingPolynomial2D>{_seq(",".join(map(str, coef)))}</Camera:VignettingPolynomial2D>'
              f'<Camera:VignettingPolynomial2DName>{_seq(",".join(map(str, powers)))}</Camera:VignettingPolynomial2DName>'
              '</rdf:Description></rdf:RDF></x:xmpmeta>')
    info = ImageFileDirectory_v2()
    info[700] = packet.encode()
    info[50714] = 240
    info[34665] = {33434: 0.001, 34867: 100}
    path = tmp_path / "IMG_0000_6.tif"
    Image.fromarray(dn).save(path, tiffinfo=info)
    tags = ms.xmp_of(path)
    tags["BitsPerSample"] = 12  # what the camera's header says (PIL writes its own on saving)
    rad, used = ms.radiance_image(path, tags)
    assert used["model"] == "micasense" and used["vignetting"]
    for y, x in ((0, 0), (30, 40), (59, 79)):
        xv, yv = x / w, y / h
        fall = 1 + 0.2 * xv - 0.1 * yv + 0.3 * xv ** 2 * yv ** 2
        want = (2000 - 240) / 4096 / fall * 1.9e-04 / 1.0 / (0.001 + 2.7e-09 * y - 1.6e-05 * 0.001 * y)
        assert rad[y, x] == pytest.approx(want, rel=1e-4)


def test_bands_of_other_cameras_start_from_their_geometry():
    """A band with its own lens (MicaSense's panchromatic band: finer and narrower, a degree apart in
    the rig): registered from the cameras' geometry onto its own pixels; a band ECC cannot match (a
    thermal band) keeps that geometry."""
    rng = np.random.default_rng(9)
    big = cv2.GaussianBlur(rng.uniform(0, 1, (900, 1200)).astype(np.float32), (0, 0), 3)
    k_ref = np.array([[500.0, 0, 200], [0, 500.0, 150], [0, 0, 1]])
    k_pan = np.array([[1100.0, 0, 410], [0, 1100.0, 300], [0, 0, 1]])
    rot = ms._rig_rotation([0.0, 0.0, 1.0])  # one degree about the optical axis
    world = np.array([[2000.0, 0, 600], [0, 2000.0, 450], [0, 0, 1]])  # a far plane, its texture in pixels

    def view(k, r, shape):
        hmg = world @ np.linalg.inv(k @ r.T)  # band pixel -> texture pixel
        return cv2.warpPerspective(big, hmg, (shape[1], shape[0]), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)

    ref = view(k_ref, np.eye(3), (300, 400))
    pan = view(k_pan, rot, (600, 820))
    cameras = {"Red": (k_ref, np.eye(3)), "Panchro": (k_pan, rot)}
    start = ms._camera_homography(cameras, "Red", "Panchro")
    k_pan_off = k_pan.copy()
    k_pan_off[0, 2] += 6  # the tags a few pixels off: ECC finds the rest
    hs = ms.register_bands({"Red": ref, "Panchro": pan}, "Red", cameras={"Red": cameras["Red"], "Panchro": (k_pan_off, rot)})
    back = ms.warp_band(pan, hs["Panchro"], ref.shape)
    inner = (slice(30, -30), slice(30, -30))
    assert np.nanmean(np.abs(back[inner] - ref[inner])) < 0.1 * np.nanstd(ref)
    assert np.allclose(start @ [200, 150, 1] / (start @ [200, 150, 1])[2], [410, 300, 1], atol=1e-6)
    noise = rng.uniform(0, 1, (75, 100)).astype(np.float32)  # nothing in common with the reference
    k_th = np.array([[125.0, 0, 50], [0, 125.0, 37.5], [0, 0, 1]])
    hs = ms.register_bands({"Red": ref, "LWIR": noise}, "Red", cameras={"Red": cameras["Red"], "LWIR": (k_th, np.eye(3))})
    assert np.allclose(hs["LWIR"], ms._camera_homography({"Red": cameras["Red"], "LWIR": (k_th, np.eye(3))}, "Red", "LWIR"))


def _fields_texture(rng, shape=(1800, 2400)) -> np.ndarray:
    """A far plane with fields, roads and buildings: rectangles of random levels (edges a thermal
    band at 1/16 of the texture's resolution still shows)."""
    tex = np.full(shape, 0.5, np.float32)
    for _ in range(140):
        h, w = rng.integers(60, 360, 2)
        y, x = rng.integers(0, shape[0] - h), rng.integers(0, shape[1] - w)
        tex[y:y + h, x:x + w] = rng.uniform(0, 1)
    return cv2.GaussianBlur(tex, (0, 0), 2.0)


def _lens_view(tex: np.ndarray, world: np.ndarray, k: np.ndarray, dist, r: np.ndarray, shape) -> np.ndarray:
    """What a band with lens (K, distortion) turned by ``r`` in the rig sees of the texture."""
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float64)
    n = ms._undistort(np.stack([xx.ravel(), yy.ravel()], 1), k, dist)
    ray = np.concatenate([n, np.ones((len(n), 1))], 1) @ r.T @ world.T  # the band's ray in the rig -> texture
    m = (ray[:, :2] / ray[:, 2:3]).reshape(shape[0], shape[1], 2).astype(np.float32)
    return cv2.remap(tex, m[..., 0], m[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def test_bands_registered_through_their_lenses_and_a_thermal_band_by_its_edges():
    """MicaSense's bands have their own lens (``PerspectiveDistortion``): every band is undistorted
    and turned by its rotation in the rig before ECC, and the registration goes from the
    reference's distorted pixels to the band's. A thermal band (a quarter of the resolution, a
    strong barrel distortion, the contrast reversed: warm roads, cool fields) whose rig rotation in
    the tags is off by about 1.4 thermal pixels is refined by the agreement of its edges, within
    half a thermal pixel; without the lens model it would be pixels off at the corners."""
    import json

    rng = np.random.default_rng(21)
    tex = _fields_texture(rng)
    world = np.array([[1600.0, 0, 1200], [0, 1600.0, 900], [0, 0, 1]])  # rig ray -> texture pixel
    k_ref = np.array([[500.0, 0, 203], [0, 500.0, 147], [0, 0, 1]])
    d_ref = np.array([-0.12, 0.2, 0.0004, -0.0009, -0.1])
    k_nir = np.array([[505.0, 0, 197], [0, 505.0, 152], [0, 0, 1]])
    d_nir = np.array([-0.13, 0.22, -0.0001, 0.0005, -0.15])
    k_th = np.array([[125.0, 0, 51.5], [0, 125.0, 37.0], [0, 0, 1]])
    d_th = np.array([-0.36, 0.23, 0.0015, -0.003, -0.03])
    r_nir = ms._rig_rotation([0.2, -0.1, 0.3])
    r_th_tags = ms._rig_rotation([-0.1, 0.34, -0.9])
    r_th = r_th_tags @ ms._rig_rotation([0.55, -0.35, 0.0])  # the tags about 1.4 thermal pixels off
    ref = _lens_view(tex, world, k_ref, d_ref, np.eye(3), (300, 400))
    nir = _lens_view(tex, world, k_nir, d_nir, r_nir @ ms._rig_rotation([0.0, 0.0, 0.05]), (300, 400))
    fine = _lens_view(tex, world, k_th * [[4], [4], [1]] + [[0, 0, 1.5], [0, 0, 1.5], [0, 0, 0]], d_th, r_th, (300, 400))
    thermal = 30.0 - 12.0 * cv2.resize(fine, (100, 75), interpolation=cv2.INTER_AREA)  # degrees: warm where dark
    thermal = cv2.GaussianBlur(thermal, (0, 0), 0.6)
    cameras = {"Red": (k_ref, np.eye(3), d_ref), "NIR": (k_nir, r_nir, d_nir), "LWIR": (k_th, r_th_tags, d_th)}
    quality: dict = {}
    hs = ms.register_bands({"Red": ref, "NIR": nir, "LWIR": thermal}, "Red", quality=quality, cameras=cameras)
    assert isinstance(hs["NIR"], dict) and isinstance(hs["LWIR"], dict) and np.allclose(hs["Red"], np.eye(3))

    def truth(k_b, d_b, r_b, pts):
        n = ms._undistort(pts, k_ref, d_ref)
        ray = np.concatenate([n, np.ones((len(n), 1))], 1) @ r_b  # r_b.T @ ray, row-wise
        return ms._distort(ray[:, :2] / ray[:, 2:3], k_b, d_b)

    gy, gx = np.mgrid[20:281:26, 20:381:36].astype(np.float64)
    pts = np.stack([gx.ravel(), gy.ravel()], 1)
    err_nir = np.linalg.norm(ms.map_points(hs["NIR"], pts) - truth(k_nir, d_nir, r_nir @ ms._rig_rotation([0, 0, 0.05]), pts), axis=1)
    assert np.median(err_nir) < 0.15 and err_nir.max() < 0.5, err_nir
    # the thermal band: the cameras' geometry alone, then refined by the edges (thermal pixels)
    want = truth(k_th, d_th, r_th, pts)
    seen = (want[:, 0] > 3) & (want[:, 0] < 97) & (want[:, 1] > 3) & (want[:, 1] < 72)
    geometry = np.linalg.norm(truth(k_th, d_th, r_th_tags, pts) - want, axis=1)[seen]
    err_th = np.linalg.norm(ms.map_points(hs["LWIR"], pts) - want, axis=1)[seen]
    assert np.median(geometry) > 1.0
    assert quality["LWIR"]["on"] == "edges" and quality["LWIR"]["correlation"] > quality["LWIR"]["before"]
    assert np.median(err_th) < 0.3 and np.percentile(err_th, 90) < 0.5, err_th
    # without the lens model (a homography from the rotations) the corners would be pixels off
    plain = ms._camera_homography({"Red": (k_ref, np.eye(3)), "LWIR": (k_th, r_th)}, "Red", "LWIR")
    err_plain = np.linalg.norm(cv2.perspectiveTransform(pts[None], plain)[0] - want, axis=1)[seen]
    assert err_plain.max() > 2.0
    # warped onto the reference, through JSON (as kept in the chunk's meta)
    back = json.loads(json.dumps(ms.registration_json(hs["NIR"])))
    warped = ms.warp_band(nir, back, ref.shape)
    inner = (slice(25, -25), slice(25, -25))
    assert np.nanmean(np.abs(warped[inner] - ref[inner])) < 0.05 * np.nanstd(ref)
    assert np.allclose(warped, ms.warp_band(nir, hs["NIR"], ref.shape), equal_nan=True)


def test_sun_position_and_the_tilt_of_a_dls_1():
    """The sun's position (NOAA's equations) against what MicaSense's DLS 2 wrote on the Altum sample
    (2022-04-06 20:00:32 UTC, 47.70364 N 122.14142 W: elevation 48.927, azimuth 176.078 degrees);
    a DLS 1 reading corrected for the sensor's tilt as MicaSense's model does it (Fresnel transmission
    of the diffuser, direct x cos(angle) + scattered = direct / 6), the horizontal irradiance found
    back; without the attitude the reading stays as written."""
    from datetime import datetime, timezone

    el, az = ms.sun_position(47.70364, -122.14142, datetime(2022, 4, 6, 20, 0, 32, tzinfo=timezone.utc))
    assert np.degrees(el) == pytest.approx(48.927, abs=0.05) and np.degrees(az) == pytest.approx(176.078, abs=0.05)
    direct, diffuse = 1.1, 1.0 / 6.0
    yaw, pitch, roll = 0.4, np.radians(8.0), np.radians(-5.0)
    sun = np.array([np.cos(az) * np.cos(el), np.sin(az) * np.cos(el), -np.sin(el)])
    angle = np.arccos(sun @ ms.sensor_normal(yaw, pitch, roll))
    reading = ms._fresnel_transmission(angle) * direct * (np.cos(angle) + diffuse)
    tags = {"Make": "MicaSense", "SpectralIrradiance": str(reading), "Yaw": str(yaw), "Pitch": str(pitch),
            "Roll": str(roll), "GPSLatitude": 47.70364, "GPSLongitude": -122.14142,
            "DateTimeOriginal": "2022:04:06 20:00:32"}
    value, how = ms.dls_irradiance(tags)
    assert how == "dls1" and value == pytest.approx(direct * (np.sin(el) + diffuse), rel=1e-3)
    assert ms._fresnel_transmission(0.0) == pytest.approx(0.942, abs=0.002)  # 6 % lost at normal incidence
    level = ms.sensor_normal(0.0, 0.0, 0.0)
    assert np.allclose(level, [0, 0, -1])  # up, in north-east-down
    no_pose = {k: v for k, v in tags.items() if k not in ("Yaw", "Pitch", "Roll")}
    assert ms.dls_irradiance(no_pose) == (pytest.approx(reading), "spectral")


def test_panel_with_the_sun_sensor_following_the_light(tmp_path):
    """A panel capture and a flight capture of a MicaSense camera with a DLS 2: the panel gives the
    level of every band, the DLS the change of light (half the light in the flight: the same
    digital numbers are twice the reflectance). The default when both are there; the method in
    ``meta["multispectral"]["radiometry"]``; the panel alone when asked; the DLS alone without a panel."""
    h, w = 60, 80
    folder = tmp_path / "SET"
    folder.mkdir()
    cal = f"<Camera:RadiometricCalibration>{_seq('9.6e-05', '8.7e-08', '3.3e-06')}</Camera:RadiometricCalibration>"
    panel = ("<Camera:CalibrationPicture>2</Camera:CalibrationPicture><Camera:Albedo>0.5</Camera:Albedo>"
             f"<Camera:ReflectArea>{_seq('50,20,50,40,30,40,30,20')}</Camera:ReflectArea>")
    rng = np.random.default_rng(5)
    dn = (8000 + 20000 * rng.uniform(0, 1, (h, w))).astype(np.uint16)
    dn[20:40, 30:50] = 30000
    for stem, irr, extra in (("IMG_0001", 80.0, panel), ("IMG_0002", 40.0, "")):
        dls = f"<DLS:HorizontalIrradiance>{irr}</DLS:HorizontalIrradiance><DLS:SpectralIrradiance>{irr}</DLS:SpectralIrradiance>"
        for k, name in (("3", "Red"), ("4", "NIR")):
            _micasense_band(folder / f"{stem}_{k}.tif", dn, name, xmp=cal + dls + extra)
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    cams = ms.add_captures(ch, [folder])
    assert ch.meta["multispectral"]["radiometry"] == "dls"
    by_capture = {c.meta["capture"]: c for c in cams}
    factors = ms.calibrate_panel(ch, by_capture["IMG_0001"])
    info = ch.meta["multispectral"]
    assert info["radiometry"] == "panel+dls" and info["panel"]["follow_dls"]
    assert info["panel"]["irradiance"]["Red"] == pytest.approx(0.8)
    ch.meta["multispectral"]["registration"] = {str(c.key): {"Red": np.eye(3).tolist(), "NIR": np.eye(3).tolist()} for c in cams}
    rad, _ = ms.radiance_image(by_capture["IMG_0002"].meta["bands"]["Red"])
    flight = ms.band_stack(ch, by_capture["IMG_0002"], ["Red"])[..., 0]
    assert np.allclose(flight, rad * factors["Red"] * 2.0, rtol=1e-5)
    at_panel = ms.band_stack(ch, by_capture["IMG_0001"], ["Red"])[..., 0]
    region = ms._region_mask(info["panel"]["regions"]["Red"], at_panel.shape)
    assert float(at_panel[region].mean()) == pytest.approx(0.5, rel=1e-5)  # the panel's capture: its albedo
    ms.calibrate_panel(ch, by_capture["IMG_0001"], follow_dls=False)
    assert ch.meta["multispectral"]["radiometry"] == "panel"
    assert np.allclose(ms.band_stack(ch, by_capture["IMG_0002"], ["Red"])[..., 0], rad * factors["Red"], rtol=1e-5)


MICASENSE_SAMPLES = Path("/Volumes/Extreme Pro/verifica_ppg/dati_pubblici/micasense")


@pytest.mark.skipif(not MICASENSE_SAMPLES.exists(), reason="MicaSense's sample captures are not on this computer")
def test_micasense_samples_against_micasenses_numbers():
    """The sample captures of MicaSense's imageprocessing repository (MIT): our radiance, DLS
    irradiance and panel against the numbers of MicaSense's own library (2026-10-24; the 12-bit
    files read with their real digital numbers, see the test above)."""
    reference = {  # band file: (mean radiance over the panel the camera found, DLS horizontal irradiance)
        "REDEDGE-MX/IMG_0001_3": (0.15775995652381508, 0.6728975021380448),
        "REDEDGE-P/IMG_0000_3": (0.14991433644810898, 0.9119319162764613),
        "ALTUM/IMG_0000_4": (0.11467415436937316, 0.45482744465926417),
        "REDEDGE-MX-DUAL/IMG_0000_9": (0.11972001092139525, 0.38018188476562503),
    }
    for key, (panel, irradiance) in reference.items():
        path = MICASENSE_SAMPLES / f"{key}.tif"
        tags = ms.xmp_of(path)
        rad, used = ms.radiance_image(path, tags)
        corners, albedo = ms.panel_in_tags(tags)
        mean = float(rad[ms._region_mask(corners, rad.shape)].mean())
        assert mean == pytest.approx(panel, rel=2e-4), key
        assert ms.irradiance_of(tags) == pytest.approx(irradiance, rel=1e-6), key
    images = {  # band file: (mean radiance of the image, at row 100 column 100) - degrees for the thermal band
        "ALTUM/IMG_0021_3": (0.016061170777141583, None),
        "ALTUM-PT/IMG_0010_6": (0.031074204038329157, None),
        "REDEDGE-P/IMG_0011_4": (0.05635678250515375, None),
        "REDEDGE-MX-DUAL/IMG_0431_10": (0.06067793713835656, 0.0588647723197937),
        "ALTUM/IMG_0021_6": (19.065110942622027, 13.520000457763672),
    }
    for key, (mean, pixel) in images.items():
        rad, _used = ms.radiance_image(MICASENSE_SAMPLES / f"{key}.tif")
        assert float(rad.astype(float).mean()) == pytest.approx(mean, rel=1e-4), key
        if pixel is not None:
            assert float(rad[100, 100]) == pytest.approx(pixel, rel=1e-4), key
    caps = ms.detect_bands([MICASENSE_SAMPLES / "ALTUM-PT"])
    assert set(caps["IMG_0010"]) == {"Blue", "Green", "Red", "NIR", "RedEdge", "Panchro", "LWIR"}
    caps = ms.detect_bands([MICASENSE_SAMPLES / "REDEDGE-MX-DUAL"])
    assert len(caps["IMG_0431"]) == 10


@pytest.mark.skipif(not MICASENSE_SAMPLES.exists(), reason="MicaSense's sample captures are not on this computer")
def test_micasense_samples_thermal_band_and_sun_sensor():
    """On MicaSense's samples (2026-10-24): the thermal band of the Altum and the Altum-PT flight
    captures refined by its edges (tile shifts against the red band 1.1-2.2 thermal pixels with the
    rig's homography, 0.24-0.5 now), the panel captures - taken at arm's length, where the rig's
    parallax is no small correction - left on the cameras' geometry; the sun's position within 0.05
    degrees of what the DLS 2 wrote; the sun sensor's change of light between the panel and the
    flight the same in every band within 2 % (it follows the light well), while its level, alone,
    gives the panel 0.64-2.2 times its albedo (MicaSense's own library gives the same)."""
    for cam, capture, method in (("ALTUM", "IMG_0021", "edges"), ("ALTUM-PT", "IMG_0010", "edges"),
                                 ("ALTUM", "IMG_0000", "cameras")):
        files = ms.detect_bands([MICASENSE_SAMPLES / cam])[capture]
        imgs = {b: ms.reflectance_image(p) for b, p in files.items()}
        tags = {b: ms.xmp_of(p) for b, p in files.items()}
        quality: dict = {}
        hs = ms.register_bands(imgs, "Red", quality=quality, cameras=ms.band_cameras(tags, {b: i.shape for b, i in imgs.items()}))
        assert quality["LWIR"]["on"] == method, (cam, capture, quality["LWIR"])
        assert all(isinstance(hs[b], dict) for b in hs if b != "Red")
        if method == "edges":
            assert quality["LWIR"]["correlation"] > quality["LWIR"]["before"] + 0.03
    for cam in ("ALTUM", "ALTUM-PT", "REDEDGE-MX", "REDEDGE-MX-DUAL", "REDEDGE-P"):
        caps = ms.detect_bands([MICASENSE_SAMPLES / cam])
        panel = next(s for s in sorted(caps) if any(ms.panel_in_tags(ms.xmp_of(p)) for p in caps[s].values()))
        flight = next(s for s in sorted(caps) if s != panel)
        ratios = []
        for band, path in caps[panel].items():
            tags = ms.xmp_of(path)
            if ms._micasense_lwir(tags):
                continue
            sun = ms._sun_of({k: v for k, v in tags.items() if k not in ("SolarElevation", "SolarAzimuth")})
            assert np.degrees(sun[0]) == pytest.approx(np.degrees(float(tags["SolarElevation"])), abs=0.05)
            assert np.degrees(sun[1]) == pytest.approx(np.degrees(float(tags["SolarAzimuth"])), abs=0.05)
            ratios.append(ms.irradiance_of(ms.xmp_of(caps[flight][band])) / ms.irradiance_of(tags))
        assert max(ratios) / min(ratios) < 1.02, (cam, ratios)


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
        # one band on its own, in grey, from the pane's choices
        k = w_.ms_pane.c_show.findData("band:NIR")
        assert k > 1 and w_.ms_pane.c_show.count() == 2 + len(bands)
        w_.ms_pane.c_show.setCurrentIndex(k)
        w_.ms_pane._show_reflectance()
        assert w_.ortho_view.kind == "reflectance" and w_.ortho_view._bands == (5, 5, 5)
        rgba = image_views.read_window_rgba(folder / "reflectance.tif", "reflectance", (0, 0, w, h), (h, w), bands=(5, 5, 5),
                                            limits=w_.ortho_view._band_limits)
        assert np.array_equal(rgba[..., 0], rgba[..., 1]) and np.array_equal(rgba[..., 1], rgba[..., 2])
        assert rgba[:, :40, 0].mean() > rgba[:, 40:, 0].mean() + 60
        w_._workspace_item("product", ch, "ms:index:NDVI")
        assert w_.ortho_view.kind == "index" and w_.ortho_view._dem_range[0] < w_.ortho_view._dem_range[1]
        rgba = image_views.read_window_rgba(ndvi, "index", (0, 0, w, h), (h, w), w_.ortho_view._dem_range)
        assert rgba[:, :40, 0].mean() < rgba[:, 40:, 0].mean() - 60  # red where low, dark green where high
        # another ramp from the pane: the view coloured again, the choice kept for the next window
        w_.ms_pane.c_palette.setCurrentIndex(w_.ms_pane.c_palette.findData("grey"))
        assert w_.ortho_view.palette == "grey" and w_.ortho_view.kind == "index"
        assert w_.settings.value("ms/palette") == "grey"
        grey = image_views.read_window_rgba(ndvi, "index", (0, 0, w, h), (h, w), w_.ortho_view._dem_range, palette="grey")
        assert np.array_equal(grey[..., 0], grey[..., 1]) and grey[:, :40, 0].mean() > grey[:, 40:, 0].mean() + 60
        for palette in ("viridis", "turbo"):
            ends = image_views.index_colors(np.array([[0.0, 1.0]]), (0.0, 1.0), palette)[0]
            assert ends[0, 3] == 255 and not np.array_equal(ends[0, :3], ends[1, :3])
        assert image_views.index_colors(np.array([[0.0, 1.0]]), (0.0, 1.0), "viridis")[0, 1, 1] > 200  # yellow end
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


def test_adding_multispectral_files_as_photos_offers_the_captures(tmp_path, monkeypatch):
    """Foto > Aggiungi cartella on the files of a multispectral camera: the window recognises
    them and offers to add them as captures, then shows the Multispectral pane (a user followed
    the ordinary steps on them, and the bands were aligned as different views)."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    rng = np.random.default_rng(7)
    roll = tmp_path / "flight" / "100MEDIA"
    roll.mkdir(parents=True)
    _p4m_capture(roll, 10, "u1", rng)
    _p4m_capture(roll, 20, "u2", rng)
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    w_ = mwmod.MainWindow(doc, autoconnect=False)
    w_.runner.show_dialog = False
    try:
        monkeypatch.setattr(mwmod.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "flight"))
        asked = []
        monkeypatch.setattr(mwmod, "choice", lambda *args, **kw: asked.append(args[3]) or 0)
        monkeypatch.setattr(w_, "_info", lambda *a, **k: None)
        w_.add_photos(folder=True)
        w_.runner.wait()
        assert len(asked) == 1 and "2" in asked[0] and "NIR" in asked[0]
        assert {c.meta["capture"] for c in ms.multispectral_cameras(ch)} == {"DJI_0011", "DJI_0021"}
        assert ch.meta["multispectral"]["captures"] == 2 and w_.left_tabs.currentWidget() is w_.ms_pane
        # ordinary photos: no question
        monkeypatch.setattr(mwmod.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "plain"))
        (tmp_path / "plain").mkdir()
        import cv2

        cv2.imwrite(str(tmp_path / "plain" / "IMG_0001.jpg"), np.zeros((40, 60, 3), np.uint8))
        w_.add_photos(folder=True)
        w_.runner.wait()
        assert len(asked) == 1 and len(ch.cameras) == 3
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
