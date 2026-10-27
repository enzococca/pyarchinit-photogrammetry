"""Heights above the geoid: PROJ never goes to the network, the EGM2008 grid is downloaded only when asked for, into the
GNSS folder of the program, and a conversion between heights above the ellipsoid and heights above the geoid without
its grid is refused with the name of the grid and how to get it - never the heights above the ellipsoid passed off as
heights above the geoid (48 m off at Rome). Every test runs in a Python of its own, with a GNSS folder of its own: PROJ
keeps in memory the grids it looked for, found or not."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROME = (12.4964, 41.9028)  # longitude, latitude

# what every script starts with: the package as the program imports it (PROJ offline, the folder of the grids in its
# search), a synthetic geoid grid written as PROJ reads EGM2008, and the message of a refusal
_PRELUDE = '''
import json, os, sys, threading
from pathlib import Path
import numpy as np
import pyarchinit_photogrammetry
import rasterio
from rasterio.transform import from_origin
from pyarchinit_photogrammetry.core import crs
from pyarchinit_photogrammetry.gnss import frames, heights

ROME = [[12.4964, 41.9028, 100.0]]


def write_grid(name="us_nga_egm08_25.tif", value=40.0, folder=None):
    """A grid of geoid heights over the whole earth (a cell a degree), as PROJ reads EGM2008: ``value`` metres
    everywhere, or the function ``value(lon, lat)``, which PROJ interpolates between the centres of the cells."""
    folder = Path(folder or heights.grid_folder())
    folder.mkdir(parents=True, exist_ok=True)
    lon, lat = np.meshgrid(np.arange(-180.0, 181.0), np.arange(90.0, -91.0, -1.0))
    data = value(lon, lat) if callable(value) else np.full(lon.shape, value)
    with rasterio.open(folder / name, "w", driver="GTiff", width=361, height=181, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-180.5, 90.5, 1.0, 1.0)) as grid:
        grid.write(data[None].astype("float32"))
        grid.update_tags(TYPE="VERTICAL_OFFSET_GEOGRAPHIC_TO_VERTICAL")
        grid.update_tags(1, UNITTYPE="metre", DESCRIPTION="geoid_undulation")


def refused(call):
    """The message of the ValueError ``call`` raises, None when it raises none."""
    try:
        call()
    except ValueError as exc:
        return str(exc)
    return None
'''


def _run(tmp_path: Path, script: str, *args: str, **env) -> dict:
    """``script`` run after the prelude by a Python of its own with the GNSS folder ``tmp_path/gnss`` (and ``args`` in
    ``sys.argv``); what it prints last, read as JSON. PROJ's own folder of the user is one of the test (never the
    user's: PROJ finds grids there too), and PROJ's network points at a port of this computer where nothing answers:
    should it be on, nothing goes out."""
    path = tmp_path / "script.py"
    path.write_text(_PRELUDE + textwrap.dedent(script))
    environment = {**os.environ, "PPG_GNSS_DIR": str(tmp_path / "gnss"), "PPG_NO_OPEN3D": "1",
                   "PROJ_USER_WRITABLE_DIRECTORY": str(tmp_path / "proj_user"),
                   "PROJ_NETWORK_ENDPOINT": "http://127.0.0.1:9", **env}
    run = subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, timeout=300,
                         env=environment)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _says_how_to_get_egm2008(message: str | None) -> None:
    assert message is not None, "converted without the geoid grid"
    assert "us_nga_egm08_25" in message
    assert "GNSS > Sistema e quote > Scarica il geoide EGM2008" in message and "ppg gnss geoid --download" in message


def test_without_the_grid_heights_are_refused(tmp_path):
    """No grid, and PROJ_NETWORK=ON in the shell (the user's Anaconda): PROJ stays offline and a height above the
    ellipsoid never becomes one above the geoid as it is - every way there refuses, naming the grid and how to get
    it; a system without heights (2D) keeps the height as it is."""
    out = _run(tmp_path, '''
        import pyproj.network
        out = {"network": pyproj.network.is_network_enabled(), "env": os.environ.get("PROJ_NETWORK"),
               "available": heights.geoid_available()}
        out["static"] = refused(lambda: crs.transform_points(ROME, "EPSG:4979", "EPSG:32633+3855"))
        out["back"] = refused(lambda: crs.transform_points([[292624.9, 4641695.9, 52.0]], "EPSG:32633+3855",
                                                           "EPSG:4979"))
        # a position in ITRF2020 at its epoch (the dynamic route), and one without (the route of the old projects)
        out["dynamic"] = refused(lambda: crs.transform_points(ROME, "EPSG:9989", "EPSG:7792+3855", epoch=2026.82))
        out["legacy"] = refused(lambda: crs.transform_points(ROME, "EPSG:9989", "EPSG:7792+3855",
                                                             legacy=frames.Legacy("test points")))
        out["national"] = refused(lambda: crs.transform_points(ROME, "EPSG:4979", "EPSG:3004+3855"))
        try:
            crs.transform_points(ROME, "EPSG:4979", "EPSG:32633+3855")
        except heights.GridMissing as exc:
            out["grids"] = list(exc.grids)
        out["plain"] = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633")[0].tolist()
        out["geoid_to_plain"] = crs.transform_points([[292624.9, 4641695.9, 52.0]], "EPSG:32633+3855",
                                                     "EPSG:32633")[0].tolist()
        print(json.dumps(out))
    ''', PROJ_NETWORK="ON")
    assert out["network"] is False and out["env"] == "OFF"  # offline, whatever the shell said
    assert out["available"] is False
    for route in ("static", "back", "dynamic", "legacy", "national"):
        _says_how_to_get_egm2008(out[route])
    assert out["grids"] == ["us_nga_egm08_25.tif"]
    assert out["plain"][2] == 100.0 and out["geoid_to_plain"][2] == 52.0  # no heights of its own: as they are


def test_relative_geometry_needs_no_geoid(tmp_path):
    """A chunk in a system with EGM2008 heights, photos with GNSS heights above the ellipsoid (an MRK), no grid: what
    needs only where the photos are relative to each other - a survey from the air or not, the pairs of photos by their
    positions, the span of the flight - needs no geoid (a height common to all does not matter) and works as in a
    system without heights. The matching needs no system either: it only warns, naming the grid and how to get it
    (the positions of the photos may be switched off before the alignment); the alignment, which puts the chunk in its
    system by them, refuses at once, before the cameras are aligned, naming the grid - unless three control markers
    place it and the GPS of the photos is not used."""
    out = _run(tmp_path, '''
        import logging
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.core.camera import Camera, Reference
        from pyarchinit_photogrammetry.engine import dense, sfm
        from pyarchinit_photogrammetry.gnss import apply as gnss_apply
        from pyarchinit_photogrammetry.markers import gcp

        def chunk(system):
            ch = ppg.Document().add_chunk()
            ch.crs = system
            for key in range(8):  # a flight of 8 photos over 300 m, 2 cm fixes, aligned: a survey from the air
                cam = Camera(key=key, label=f"c{key}", path=str(Path(sys.argv[1]) / f"c{key}.jpg"), sensor_key=0)
                gnss_apply.set_camera_position(cam, [12.4964 + key % 4 * 0.001, 41.9028 + key // 4 * 0.001, 100.0],
                                               "EPSG:4979", [0.02, 0.02, 0.04], "mrk", status="fix",
                                               height="ellipsoidal")
                cam.transform = np.eye(4)
                cam.transform[:3, 3] = [key % 4 * 83.0, key // 4 * 111.0, 0.0]
                ch.cameras.append(cam)
            return ch

        out = {}
        for system in ("EPSG:32633", "EPSG:32633+3855"):
            ch = chunk(system)
            names = {c.key: f"{c.label}.jpg" for c in ch.cameras}
            out[system] = {"aerial": dense.aerial_survey(ch), "mapper": sfm.auto_mapper(ch, 100),
                           "span": gcp.cameras_span_their_accuracy(ch),
                           "pairs": len(sfm._reference_pairs(ch, names, neighbors=3) or [])}
        ch = chunk("EPSG:32633+3855")
        warnings = []
        catcher = logging.Handler(logging.WARNING)
        catcher.emit = lambda record: warnings.append(record.getMessage())
        logging.getLogger("pyarchinit_photogrammetry").addHandler(catcher)
        sfm_dir = sfm._sfm_dir

        def past_the_check(chunk):  # where the work of the matching starts
            raise RuntimeError("past the check")

        sfm._sfm_dir = past_the_check
        try:
            ch.match_photos()
        except RuntimeError as exc:
            out["match"] = str(exc)
        sfm._sfm_dir = sfm_dir
        out["match_warnings"] = [w for w in warnings if "us_nga_egm08_25" in w]
        odd = chunk("EPSG:32633")  # positions in a system PROJ does not know: the matching says so, and goes on
        for cam in odd.cameras:
            cam.meta["reference_crs"] = "EPSG:999999"
        warnings.clear()
        sfm._sfm_dir = past_the_check
        try:
            odd.match_photos()
        except RuntimeError as exc:
            out["odd"] = str(exc)
        sfm._sfm_dir = sfm_dir
        out["odd_warnings"] = len(warnings)
        out["align"] = refused(lambda: ch.align_cameras(use_gps=False))
        out["align_gps"] = refused(lambda: ch.align_cameras())
        out["started"] = "sfm" in ch.meta  # nothing was started
        for i in range(3):  # three control markers, in the system of the chunk, seen by two photos each
            m = ch.add_marker(f"P{i}")
            m.reference = Reference(location=[292400.0 + 50 * i, 4642100.0 + 30 * i, 52.0], accuracy=[0.01] * 3)
            for cam in ch.cameras[i:i + 2]:
                ch.add_marker_projection(m, cam, 100.0, 100.0)
        try:
            ch.align_cameras(use_gps=False)
        except Exception as exc:  # past the check: to the work, which needs the matches
            out["markers_place_it"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(out))
    ''', str(tmp_path / "photos"))
    plain, geoid = out["EPSG:32633"], out["EPSG:32633+3855"]
    assert plain == geoid and plain["aerial"] is True and plain["mapper"] == "global" and plain["span"] is True
    assert plain["pairs"] > 0
    assert out.get("match") == "past the check"  # warned, not refused
    assert out.get("odd") == "past the check" and out["odd_warnings"] == 1
    assert len(out["match_warnings"]) == 1
    _says_how_to_get_egm2008(out["match_warnings"][0])
    assert out["started"] is False
    _says_how_to_get_egm2008(out["align"])
    _says_how_to_get_egm2008(out["align_gps"])
    assert out["markers_place_it"].startswith("RuntimeError") and "match_photos" in out["markers_place_it"]


def test_markers_alone_place_a_chunk_whose_photo_references_are_off(scene_dir, tmp_path):
    """The chunk in a system with EGM2008 heights, the GNSS positions of the photos (heights above the ellipsoid)
    switched off, the control points typed in the system of the chunk, no grid: nothing has to go through the geoid -
    the photos are matched and aligned and the markers place the chunk, as in a system without heights. Switched on
    again, the positions of the photos would hold the alignment: it refuses before it starts, naming the grid, and the
    alignment there is stays as it is."""
    out = _run(tmp_path, '''
        import logging
        from pyproj import Transformer
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.gnss import apply as gnss_apply
        from pyarchinit_photogrammetry.testing.synthetic import camera_poses, make_scene

        scene_dir, folder = Path(sys.argv[1]), Path(sys.argv[2])
        warnings = []
        catcher = logging.Handler(logging.WARNING)
        catcher.emit = lambda record: warnings.append(record.getMessage())
        logging.getLogger("pyarchinit_photogrammetry").addHandler(catcher)

        doc = ppg.Document()
        ch = doc.add_chunk("Saggio")
        doc.save(folder / "project.ppg")
        ch.crs = "EPSG:32633+3855"
        ch.add_photos([scene_dir / "photos"])
        ch.detect_markers("aruco_4x4_50")
        scene = make_scene()  # the trench of the photos, in Gauss-Boaga
        poses = camera_poses(scene)
        to_lonlat = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True)
        for cam in ch.cameras:  # where the photos were taken, with heights above the ellipsoid: switched off
            x, y, z = poses[int(cam.label.split("_")[-1])][:3, 3] + np.asarray(scene.origin)
            gnss_apply.set_camera_position(cam, [*to_lonlat.transform(x, y), z + 48.0], "EPSG:4979",
                                           [0.02, 0.02, 0.04], "mrk", status="fix", height="ellipsoidal")
            cam.reference.enabled = False
        to_utm = Transformer.from_crs(scene.crs, "EPSG:32633", always_xy=True)
        typed = folder / "gcp_utm.csv"  # the control points typed in the system of the chunk
        typed.write_text("".join(f"{code},{','.join(str(v) for v in [*to_utm.transform(x, y), z])}\\n"
                                 for code, (x, y, z) in scene.targets.items()))
        ch.import_reference(typed, crs=ch.crs)
        ch.match_photos(quality="high")
        aligned = ch.align_cameras()
        report = ch.update_transform()
        out = {"photos": len(ch.cameras), "aligned": aligned, "markers": len(ch.markers), "method": report["method"],
               "control": report["control_rmse"], "crs": ch.crs, "priors": ch.meta["sfm"]["gps_priors"],
               "grid": heights.geoid_available(), "warnings": warnings}
        before = ch.transform.copy()
        for cam in ch.cameras:
            cam.reference.enabled = True
        out["switched_on"] = refused(lambda: ch.align_cameras())
        out["kept"] = bool(np.array_equal(before, ch.transform)) and len(ch.aligned_cameras) == aligned
        print(json.dumps(out))
    ''', str(scene_dir), str(tmp_path))
    assert out["grid"] is False
    assert out["aligned"] == out["photos"] and out["markers"] == 8 and out["priors"] is None
    assert out["crs"] == "EPSG:32633+3855" and out["method"] == "markers" and out["control"] < 0.002
    assert not [w for w in out["warnings"] if "us_nga_egm08_25" in w]
    _says_how_to_get_egm2008(out["switched_on"])
    assert out["kept"] is True


def test_the_exports_on_the_globe_take_heights_above_the_ellipsoid(tmp_path):
    """3D Tiles and I3S place the model on the globe by heights above the ellipsoid of WGS 84: from a system with
    EGM2008 heights they are converted with the grid (40 m higher with a geoid of 40 m), refused without it; from a
    system without heights of its own they go as they are, as before. I3S takes a system with EGM2008 heights (its plan
    is projected, with an EPSG code)."""
    out = _run(tmp_path, '''
        from pyproj import Transformer
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.io import i3s, web

        utm = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633")[0]
        centre = [utm[0], utm[1], 60.0]
        to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)

        def placed(system):
            ch = ppg.Document().add_chunk()
            ch.crs = system
            return [web._enu_to_ecef(ch, centre)[12:15], i3s._Global(system).geo(np.array([centre]))[0].tolist()]

        out = {"epsg": i3s.projected_epsg("EPSG:32633+3855"), "plain": placed("EPSG:32633"),
               "refused": refused(lambda: placed("EPSG:32633+3855"))}
        write_grid(value=40.0)
        out["geoid"] = placed("EPSG:32633+3855")
        out["expected"] = [list(to_ecef.transform(*ROME[0][:2], h)) for h in (60.0, 100.0)]
        out["rome"] = ROME[0][:2]
        print(json.dumps(out))
    ''')
    assert out["epsg"] == 32633
    _says_how_to_get_egm2008(out["refused"])
    ecef60, ecef100 = out["expected"]
    tiles, geo = out["plain"]
    assert tiles == pytest.approx(ecef60, abs=1e-3) and geo == pytest.approx([*out["rome"], 60.0], abs=1e-6)
    tiles, geo = out["geoid"]
    assert tiles == pytest.approx(ecef100, abs=1e-3) and geo == pytest.approx([*out["rome"], 100.0], abs=1e-6)


def test_the_folder_of_the_grids_leaves_proj_data_to_rasterio(tmp_path):
    """The folder of the grids is in pyproj's search, not in PROJ_DATA: rasterio hands PROJ_DATA to GDAL as one folder
    at every rasterio.open, and with two folders there GDAL lost proj.db (every GeoTIFF with a system, in this process
    and in the processes it starts). A shell with its own PROJ_DATA (Anaconda's), as the user's."""
    rasterio = pytest.importorskip("rasterio")
    if not (Path(rasterio.__file__).parent / "proj_data" / "proj.db").exists():
        pytest.skip("rasterio without bundled PROJ data")
    foreign = tmp_path / "anaconda_proj"
    foreign.mkdir()
    (foreign / "proj.db").write_bytes(b"not a database")
    out = _run(tmp_path, '''
        import pyproj.datadir
        out = {"proj_data": os.environ["PROJ_DATA"],
               "searched": str(heights.grid_folder()) in pyproj.datadir.get_data_dir().split(os.pathsep)}
        write_grid(value=40.0)  # a GeoTIFF with its system, written by rasterio ...
        done = {}
        thread = threading.Thread(target=lambda: done.update(read=str(rasterio.open(heights.grid_folder()
                                                                                    / heights.GEOID_GRID).crs)))
        thread.start()
        thread.join()
        out["read"] = done.get("read")  # ... and read in another thread
        out["height"] = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633+3855")[0, 2]
        print(json.dumps(out))
    ''', PROJ_DATA=str(foreign), PROJ_LIB=str(foreign))
    assert os.pathsep not in out["proj_data"] and Path(out["proj_data"]) == Path(rasterio.__file__).parent / "proj_data"
    assert out["searched"] and out["read"] == "EPSG:4326"
    assert out["height"] == pytest.approx(60.0, abs=1e-3)


def test_synthetic_grid_gives_orthometric_heights(tmp_path):
    """A geoid grid of 40 m put in the folder of the grids (as the download does) gives heights 40 m lower, in every
    thread - also one that looked for the grid before it was there - and back again; a position in ITRF2020 into
    RDN2008 with EGM2008 heights gets the same height (the Helmert moves it by centimetres)."""
    out = _run(tmp_path, '''
        import queue
        jobs, done = queue.Queue(), queue.Queue()

        def worker():  # a thread that keeps running, as those of the interface
            while (job := jobs.get()) is not None:
                try:
                    done.put(job())
                except ValueError as exc:
                    done.put(str(exc))

        early = threading.Thread(target=worker, daemon=True)  # a failure of the script never waits for it
        early.start()
        to_geoid = lambda: crs.transform_points(ROME, "EPSG:4979", "EPSG:32633+3855")[0].tolist()
        out = {"before": refused(to_geoid)}
        jobs.put(to_geoid)
        out["early_before"] = done.get()
        write_grid(value=40.0)
        out["folder"] = str(heights.grid_folder())
        out["main"] = to_geoid()
        jobs.put(to_geoid)
        out["early_after"] = done.get()
        jobs.put(None)
        early.join()
        new = {}
        thread = threading.Thread(target=lambda: new.update(value=to_geoid()))
        thread.start()
        thread.join()
        out["new_thread"] = new["value"]
        out["back"] = crs.transform_points([out["main"]], "EPSG:32633+3855", "EPSG:4979")[0].tolist()
        out["dynamic"] = crs.transform_points(ROME, "EPSG:9989", "EPSG:7792+3855", epoch=2026.82)[0].tolist()
        out["available"] = heights.geoid_available()
        print(json.dumps(out))
    ''')
    _says_how_to_get_egm2008(out["before"])
    _says_how_to_get_egm2008(out["early_before"])
    assert Path(out["folder"]) == tmp_path / "gnss" / "proj"
    for where in ("main", "early_after", "new_thread"):
        assert isinstance(out[where], list), (where, out[where])
        assert out[where][2] == pytest.approx(60.0, abs=1e-3), where
    assert out["back"][:2] == pytest.approx(list(ROME), abs=1e-9) and out["back"][2] == pytest.approx(100.0, abs=1e-3)
    assert out["dynamic"][2] == pytest.approx(60.0, abs=0.05)
    assert out["available"] is True


def test_undulation(tmp_path):
    """N = h - H, from EGM2008 at a latitude and a longitude (in this order): refused without the grid, read from it
    with it (a grid that varies with both, so that they cannot be swapped)."""
    out = _run(tmp_path, '''
        out = {"without": refused(lambda: heights.undulation(41.9028, 12.4964))}
        write_grid(value=lambda lon, lat: lat + lon / 1000.0)
        out["rome"] = heights.undulation(41.9028, 12.4964)
        out["off_the_earth"] = refused(lambda: heights.undulation(95.0, 12.4964))
        print(json.dumps(out))
    ''')
    _says_how_to_get_egm2008(out["without"])
    assert out["rome"] == pytest.approx(41.9028 + 12.4964 / 1000.0, abs=1e-3)
    assert "latitude" in out["off_the_earth"]


def test_survey_crs(tmp_path):
    """The system of a survey where it is: WGS 84 / UTM for WGS 84; for ETRF2000 RDN2008 / UTM in Italy (zones 32, 33
    and 34), ETRS89 / UTM elsewhere in Europe, WGS 84 / UTM beyond; with heights above EGM2008 or none of their own."""
    out = _run(tmp_path, '''
        places = {"rome": (12.4964, 41.9028), "milan": (9.19, 45.4642), "lecce": (18.1718, 40.3515),
                  "paris": (2.3522, 48.8566), "new_york": (-74.006, 40.7128), "santiago": (-70.6693, -33.4489),
                  "cagliari": (9.1217, 39.2238), "palermo": (13.3615, 38.1157),
                  # in the box of the area of use of RDN2008, not in Italy
                  "bern": (7.4474, 46.948), "geneva": (6.1432, 46.2044), "nice": (7.262, 43.7102),
                  "ajaccio": (8.7369, 41.9192), "tunis": (10.1815, 36.8065), "ljubljana": (14.5058, 46.0569),
                  "klagenfurt": (14.3053, 46.6247), "zagreb": (15.9819, 45.815), "valletta": (14.5146, 35.8989),
                  "sarajevo": (18.4131, 43.8563)}
        # Italian coasts: Cilento, the gulf of Policastro, Calabria on both seas, the strait, south-east Sicily
        coast = {"velia": (15.154, 40.16), "palinuro": (15.284, 40.036), "agropoli": (14.993, 40.35),
                 "castellabate": (14.948, 40.285), "acciaroli": (15.031, 40.176), "punta licosa": (14.906, 40.251),
                 "marina di camerota": (15.374, 40.003), "policastro": (15.52, 40.075), "scario": (15.49, 40.054),
                 "diamante": (15.817, 39.678), "belvedere marittimo": (15.862, 39.627), "cirella": (15.805, 39.7),
                 "kaulon": (16.565, 38.451), "riace marina": (16.534, 38.406), "caulonia marina": (16.47, 38.345),
                 "punta stilo": (16.58, 38.453), "capo rizzuto": (17.093, 38.896), "ciro marina": (17.128, 39.367),
                 "punta alice": (17.155, 39.398), "cariati": (16.955, 39.495), "roseto capo spulico": (16.618, 39.981),
                 "melito di porto salvo": (15.783, 37.918), "lago di faro": (15.637, 38.266),
                 "capo peloro": (15.652, 38.268), "villa san giovanni": (15.637, 38.22), "messina": (15.554, 38.193),
                 "punta secca": (14.493, 36.787), "marina di ragusa": (14.551, 36.783)}
        out = {"coast": {name: [heights.in_italy(lon, lat), heights.survey_crs(lon, lat, "ETRF2000")]
                         for name, (lon, lat) in coast.items()}}
        for name, (lon, lat) in places.items():
            for frame in ("WGS84", "ETRF2000"):
                out[f"{name} {frame}"] = heights.survey_crs(lon, lat, frame)
        out["rome plain"] = heights.survey_crs(12.4964, 41.9028, "ETRF2000", geoid=False)
        out["rome itrf2020"] = heights.survey_crs(12.4964, 41.9028, "ITRF2020")
        out["refused"] = refused(lambda: heights.survey_crs(12.4964, 41.9028, "NAD83"))
        out["off"] = refused(lambda: heights.survey_crs(12.4964, 95.0))
        print(json.dumps(out))
    ''')
    assert out["rome ETRF2000"] == "EPSG:7792+3855"
    assert out["milan ETRF2000"] == "EPSG:7791+3855"
    assert out["new_york WGS84"] == "EPSG:32618+3855"
    assert out["lecce ETRF2000"] == "EPSG:7793+3855"  # Salento, east of 18 E
    assert out["cagliari ETRF2000"] == "EPSG:7791+3855" and out["palermo ETRF2000"] == "EPSG:7792+3855"
    for city in ("bern", "geneva", "nice", "ajaccio", "tunis"):  # ETRS89 / UTM 32N, not RDN2008
        assert out[f"{city} ETRF2000"] == "EPSG:25832+3855", city
    for city in ("ljubljana", "klagenfurt", "zagreb", "valletta"):
        assert out[f"{city} ETRF2000"] == "EPSG:25833+3855", city
    assert out["sarajevo ETRF2000"] == "EPSG:25834+3855"
    assert out["rome WGS84"] == "EPSG:32633+3855" and out["milan WGS84"] == "EPSG:32632+3855"
    assert out["paris ETRF2000"] == "EPSG:25831+3855"  # ETRS89 / UTM 31N outside Italy
    assert out["new_york ETRF2000"] == "EPSG:32618+3855"  # no ETRS89 / UTM there
    assert out["santiago WGS84"] == out["santiago ETRF2000"] == "EPSG:32719+3855"  # south
    assert out["rome plain"] == "EPSG:7792"
    assert out["rome itrf2020"] == "EPSG:32633+3855"  # ITRF2020 is WGS 84 for the program
    assert "NAD83" in out["refused"] and "latitude" in out["off"]
    for place, (italy, system) in out["coast"].items():  # all in zone 33: RDN2008 / UTM 33N
        assert italy and system == "EPSG:7792+3855", place


def test_check_warns_for_ellipsoidal_heights_in_a_2d_crs(tmp_path):
    """Photos with heights above the ellipsoid (GNSS) in a chunk whose system has no heights of its own: the heights
    of the chunk are above the ellipsoid, said with the way out (a system with EGM2008 heights); a system with
    heights above the geoid whose grid is missing, and photos whose heights above the ellipsoid would go into it as
    they are, said too; nothing to say otherwise."""
    out = _run(tmp_path, '''
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.core.camera import Camera
        from pyarchinit_photogrammetry.gnss import apply as gnss_apply

        def chunk(system, crs_of_photos="EPSG:4979", height="ellipsoidal"):
            ch = ppg.Document().add_chunk()
            ch.crs = system
            for key in range(3):
                cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
                gnss_apply.set_camera_position(cam, [12.4964 + key * 1e-4, 41.9028, 100.0], crs_of_photos,
                                               [0.02, 0.02, 0.04], "mrk", status="fix", height=height)
                ch.cameras.append(cam)
            return ch

        from pyarchinit_photogrammetry.gnss import quality

        out = {"plain": heights.check(chunk("EPSG:32633")), "exif": heights.check(chunk("EPSG:32633", "EPSG:4326",
                                                                                        "exif")),
               "local": heights.check(chunk("LOCAL")), "no_grid": heights.check(chunk("EPSG:32633+3855")),
               "through_2d": heights.check(chunk("EPSG:7792+3855", "EPSG:4326")),
               "sea_level_in_4979": heights.check(chunk("EPSG:32633", "EPSG:4979", "exif")),
               "marked": len(heights.exif_heights_in_ellipsoidal_systems(chunk("EPSG:32633", "EPSG:4979", "exif"))),
               # the flights summed up (the table of the GNSS pane) without the residuals the grid would give
               "flights": [(f["photos"], f["residual_rms"]) for f in quality.summary(chunk("EPSG:32633+3855"))]}
        write_grid(value=40.0)
        out["with_grid"] = heights.check(chunk("EPSG:32633+3855"))
        print(json.dumps(out))
    ''')
    (said,) = out["plain"]
    assert "EPSG:32633" in said and "3 photos" in said and "ellipsoid" in said and "EGM2008" in said
    assert "sea level" in said
    assert out["exif"] == [] and out["local"] == [] and out["with_grid"] == []
    (said,) = out["no_grid"]
    _says_how_to_get_egm2008(said)
    (said,) = out["through_2d"]
    assert "EPSG:4326" in said and "EPSG:4979" in said and "3 photos" in said
    assert out["flights"] == [[3, None]]
    (said,) = out["sea_level_in_4979"]  # sea-level heights in a system that says they are above the ellipsoid
    assert "3 photos" in said and "marked as heights above the sea level" in said and "EPSG:4979" in said
    assert "EPSG:4326" in said and out["marked"] == 3


def test_download_geoid_checks_size_and_sha256(tmp_path):
    """The grid comes from its address (here a file: never the network in the tests), whole and the very file that was
    published - its size and its SHA-256 - or not at all: nothing is left in the folder of the grids, which PROJ
    searches. Once there, it is not downloaded again."""
    published = tmp_path / "published.tif"
    published.write_bytes(b"the bytes of the published grid " * 1000)
    out = _run(tmp_path, '''
        import hashlib
        from pyarchinit_photogrammetry.gnss.download import DownloadError
        import pyproj.datadir

        out = {"constants": [heights.GEOID_GRID, heights.GEOID_URL, heights.GEOID_BYTES, heights.GEOID_SHA256]}
        published = Path(sys.argv[1])
        data = published.read_bytes()
        heights.GEOID_URL = published.as_uri()

        def attempt():
            try:
                return str(heights.download_geoid())
            except DownloadError as exc:
                return "refused: " + str(exc)

        def left():
            return sorted(p.name for p in heights.grid_folder().glob("*")) if heights.grid_folder().exists() else []

        heights.GEOID_SHA256, heights.GEOID_BYTES = "0" * 64, len(data)
        out["wrong_sha"], out["left_wrong_sha"] = attempt(), left()
        heights.GEOID_SHA256, heights.GEOID_BYTES = hashlib.sha256(data).hexdigest(), len(data) + 1
        out["wrong_size"], out["left_wrong_size"] = attempt(), left()
        heights.GEOID_BYTES = len(data)
        out["right"], out["left_right"] = attempt(), left()
        out["same"] = Path(out["right"]).read_bytes() == data
        out["searched"] = pyproj.datadir.get_data_dir().split(os.pathsep).count(str(heights.grid_folder())) == 1
        heights.GEOID_URL = (published.parent / "gone.tif").as_uri()
        out["again"] = attempt()  # already there: nothing asked
        print(json.dumps(out))
    ''', str(published))
    assert out["constants"] == ["us_nga_egm08_25.tif", "https://cdn.proj.org/us_nga_egm08_25.tif", 80585622,
                                "4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a"]
    assert out["wrong_sha"].startswith("refused: ") and "SHA-256" in out["wrong_sha"]
    assert out["wrong_size"].startswith("refused: ") and "bytes" in out["wrong_size"]
    assert out["left_wrong_sha"] == [] and out["left_wrong_size"] == []
    grid = tmp_path / "gnss" / "proj" / "us_nga_egm08_25.tif"
    assert Path(out["right"]) == grid and out["left_right"] == ["us_nga_egm08_25.tif"] and out["same"]
    assert out["searched"]
    assert Path(out["again"]) == grid


def test_set_survey_crs(tmp_path):
    """A chunk aligned and placed by the GNSS positions of its photos goes into another system: the georeference is
    fitted again on the positions converted into it (a photo given in the system of the chunk, as the old projects
    have them, keeps that system), the markers' coordinates are converted, the precision of the old fit is dropped and
    the products built in the old system are named to be built again; a system with EGM2008 heights is refused, and
    nothing changes, for positions read in systems of two kinds of height (four in EPSG:4979, one in the system of the
    chunk); the same chunk with no reference left has its georeference converted (a similarity, within a millimetre of
    the conversion); a chunk in a local frame only takes the system; a system in feet is refused."""
    out = _run(tmp_path, '''
        import copy
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.core.camera import Camera, Reference
        from pyarchinit_photogrammetry.gnss import apply as gnss_apply

        def state(ch):
            return json.dumps([ch.crs, ch.transform.tolist(), [m.reference.to_dict() for m in ch.markers],
                               [c.meta for c in ch.cameras], ch.meta, ch.products], sort_keys=True)

        places = [[12.4964 + dx, 41.9028 + dy, 100.0 + dz] for dx, dy, dz in
                  [(0, 0, 0), (0.001, 0, 1), (0, 0.001, 2), (0.001, 0.001, 0.5), (0.0005, 0.0015, 3)]]
        utm = crs.transform_points(places, "EPSG:4979", "EPSG:32633")
        ch = ppg.Document().add_chunk()
        ch.crs = "EPSG:32633"
        for key, (place, at) in enumerate(zip(places, utm)):
            cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
            if key < 4:
                gnss_apply.set_camera_position(cam, place, "EPSG:4979", [0.02, 0.02, 0.04], "mrk", status="fix",
                                               height="ellipsoidal")
            else:  # a position in the system of the chunk, without a system of its own (an old project)
                cam.reference = Reference(location=list(at), accuracy=[0.02, 0.02, 0.04], enabled=True)
            cam.transform = np.eye(4)
            cam.transform[:3, 3] = at - utm.mean(axis=0)
            ch.cameras.append(cam)
        marker = ch.add_marker("P1")
        marker.reference = Reference(location=list(utm[0] + [1.0, 2.0, -3.0]), accuracy=[0.01] * 3, enabled=True)
        out = {"fitted": ch.update_transform()["method"]}
        bare = copy.deepcopy(ch)  # the same chunk, placed, without a reference left: nothing fits it again
        for cam in bare.cameras:
            cam.reference = Reference()
        bare.markers = []
        ch.products.update({"dem": "dem.tif", "orthomosaic": "orthomosaic.tif", "point_cloud": "point_cloud.ply",
                            "tie_points": "tie_points.ply", "dem_precision": "dem_precision.tif"})
        ch.meta["precision"] = {"median_m": 0.01}
        before = state(ch)
        out["refused"] = refused(lambda: heights.set_survey_crs(ch, "EPSG:7792+3855"))
        out["unchanged"] = state(ch) == before
        result = heights.set_survey_crs(ch, "EPSG:32632")
        out["result"] = {k: result[k] for k in ("crs", "updated", "rebuild")}
        out["method"], out["crs"] = result["report"]["method"], ch.crs
        out["precision"] = ["precision" in ch.meta, "dem_precision" in ch.products]
        out["pinned"] = ch.cameras[4].meta.get("reference_crs")
        expected = crs.transform_points(places, "EPSG:4979", "EPSG:32632")
        out["cameras"] = float(np.abs(np.array([ch.to_world(c.center) for c in ch.cameras]) - expected).max())
        out["marker"] = float(np.abs(np.array(marker.reference.location)
                                     - crs.transform_points([utm[0] + [1.0, 2.0, -3.0]], "EPSG:32633",
                                                            "EPSG:32632")[0]).max())
        out["geographic"] = refused(lambda: heights.set_survey_crs(ch, "EPSG:4326"))
        before = state(ch)
        out["not_projected"] = [refused(lambda: heights.set_survey_crs(ch, system))
                                for system in ("EPSG:4978", "EPSG:3855", "EPSG:4326+3855")]
        out["feet"] = refused(lambda: heights.set_survey_crs(ch, "EPSG:2263"))  # NAD83 / New York Long Island (ftUS)
        out["still"] = state(ch) == before
        where = np.array([bare.to_world(c.center) for c in bare.cameras])
        preview = heights.survey_crs_changes(bare, "EPSG:32632")
        result = heights.set_survey_crs(bare, "EPSG:32632")
        out["bare"] = {"preview": [preview["refit"], preview["transform_rms"]],
                       "result": [result["updated"], result["transform_rms"], bare.crs],
                       "cameras": float(np.abs(np.array([bare.to_world(c.center) for c in bare.cameras])
                                               - crs.transform_points(where, "EPSG:32633", "EPSG:32632")).max())}
        crs.horizontal("EPSG:32633+3855")  # kept: asked for every geometry drawn
        hits = crs.horizontal.cache_info().hits
        out["horizontal_kept"] = crs.horizontal("EPSG:32633+3855") == "EPSG:32633" and (
            crs.horizontal.cache_info().hits == hits + 1)
        loose = ppg.Document().add_chunk()  # in a local frame: it only takes the system
        result = heights.set_survey_crs(loose, "EPSG:7792+3855")
        out["loose"] = {k: result[k] for k in ("crs", "updated", "rebuild")}
        print(json.dumps(out))
    ''')
    assert out["fitted"] == "cameras"
    assert "different kinds" in out["refused"] and "4 photo positions in EPSG:4979" in out["refused"]
    assert "1 photo position in EPSG:32633" in out["refused"] and out["unchanged"]
    assert out["result"] == {"crs": "EPSG:32632", "updated": True, "rebuild": ["dem", "orthomosaic", "dem_precision"]}
    assert out["method"] == "cameras" and out["crs"] == "EPSG:32632"
    assert out["precision"] == [False, False]  # the precision of the old fit dropped
    assert out["pinned"] == "EPSG:32633"
    assert out["cameras"] < 0.01 and out["marker"] < 1e-6
    assert "geographic" in out["geographic"]
    geocentric, vertical, geographic = out["not_projected"]  # only a projected plan: EPSG:4978 was taken
    assert "geocentric" in geocentric and "vertical" in vertical and "geographic" in geographic and out["still"]
    assert "metre" in out["feet"] and "foot" in out["feet"]
    bare = out["bare"]  # its georeference converted: the cameras where the conversion takes them, within a millimetre
    assert bare["preview"][0] is False and bare["preview"][1] < 0.001
    assert bare["result"] == [False, bare["preview"][1], "EPSG:32632"] and bare["cameras"] < 0.001
    assert out["horizontal_kept"]
    assert out["loose"] == {"crs": "EPSG:7792+3855", "updated": False, "rebuild": []}


# a chunk aligned and placed by the GNSS positions of its photos (an MRK), in EPSG:32633: what a survey is before the
# system is changed; ``utm`` the positions of the photos in it
_SURVEYED = '''
import copy
import shapely
import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.camera import Camera, Reference
from pyarchinit_photogrammetry.engine.region import Region
from pyarchinit_photogrammetry.gnss import apply as gnss_apply

places = [[12.4964 + dx, 41.9028 + dy, 100.0 + dz] for dx, dy, dz in
          [(0, 0, 0), (0.001, 0, 1), (0, 0.001, 2), (0.001, 0.001, 0.5)]]
utm = crs.transform_points(places, "EPSG:4979", "EPSG:32633")


def surveyed(doc=None, system="EPSG:4979", height="ellipsoidal"):
    ch = (doc or ppg.Document()).add_chunk()
    ch.crs = "EPSG:32633"
    for key, (place, at) in enumerate(zip(places, utm)):
        cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
        gnss_apply.set_camera_position(cam, place if crs.is_geographic(system) else list(at), system,
                                       [0.02, 0.02, 0.04], "mrk" if height == "ellipsoidal" else "exif", status="fix",
                                       height=height)
        cam.transform = np.eye(4)
        cam.transform[:3, 3] = at - utm.mean(axis=0)
        ch.cameras.append(cam)
    ch.update_transform()
    return ch
'''


def test_set_survey_crs_converts_what_was_drawn_on_the_chunk(tmp_path):
    """Into another system go also what was drawn on the products of the chunk - the units, the spot heights, the
    measuring points, the stones of the orthomosaic, the sections of the project, the patches of the orthomosaic and
    of the texture, the colour chart (their plan by the conversion of the systems, the heights by the rule of the
    conversions) - and the region of the reconstruction, which follows the chunk; what cannot be converted (versions,
    elevations, rasters of other products, proxies, the drawings of the sections) is listed as kept in the old system.
    What would change can be asked before (nothing changes then), and a refusal leaves everything as it was, the file
    of the sections too."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        folder = Path(sys.argv[1])
        doc = ppg.Document()
        ch = surveyed(doc)
        doc.save(folder / "scavo.ppg")
        x0, y0, _ = utm.mean(axis=0)
        square = [(x0, y0), (x0 + 5, y0), (x0 + 5, y0 + 5), (x0, y0 + 5)]
        wkt = shapely.Polygon(square).wkt
        ch.meta["drawings"] = {
            "units": [{"wkt": wkt, "sito": "S", "area": "1", "us": "1", "tipo": "US", "attributes": {}}],
            "quote": [{"x": x0 + 1, "y": y0 + 1, "quota_q": 99.5, "sito_q": "S"}],
            "points": [{"x": x0 + 2, "y": y0 + 2, "z": 98.25, "temp_c": 20.5}]}
        ch.products.update({"orthomosaic": "orthomosaic.tif", "dem": "dem.tif"})
        triangle = shapely.Polygon([(0, 0), (1, 0), (1, 1)]).wkt  # a stone of an elevation, in its own frame
        ch.meta["stones"] = {"orthomosaic.tif": [wkt], "prospetto_nord.tif": [triangle]}
        patch = {"id": "p1", "polygon": [list(p) for p in square], "photos": ["c0"], "mode": "assign", "surface": "dem"}
        ch.meta["orthomosaic"] = {"patches": [dict(patch)], "applied_patches": [dict(patch)]}
        ch.meta["texture"] = {"patches": [{"faces": 10, "photos": ["c1"], "mode": "assign",
                                           "polygon": [list(p) for p in square]}]}
        region = Region([x0, y0, 95.0], [40.0, 30.0, 20.0], np.eye(3))
        ch.meta["region"] = region.to_dict()
        ch.meta["colour_chart"] = {"ccm": [[1, 0, 0]], "loss": 0.1, "bounds": [x0, y0, x0 + 1, y0 + 0.5]}
        ch.meta["versions"] = {"dem": [{"id": "v1", "name": "DEM del 30/09"}], "point_cloud": [{"id": "v2",
                                                                                              "name": "Nuvola"}]}
        ch.meta["elevations"] = [{"id": "e1", "name": "Prospetto nord"}]
        ch.meta["multispectral"] = {"reflectance": "multispectral/reflectance.tif",
                                    "indices": {"NDVI": {"file": "multispectral/NDVI.tif"}}}
        ch.meta["thermal"] = {"file": "multispectral/thermal.tif"}
        ch.meta["proxies"] = [{"name": "US1", "file": "proxies/US1.ply"}]
        sections_file = folder / "sezioni" / "sezioni.json"
        sections_file.parent.mkdir()
        sections_file.write_text(json.dumps([{"label": "Sezione 1", "p1": [x0, y0], "p2": [x0 + 10, y0], "length": 10.0,
                                              "svg": "s1.svg", "dxf": "s1.dxf", "kind": "vertical"}]))

        def state(c):
            return json.dumps([c.crs, c.transform.tolist(), c.meta, sections_file.read_text()], sort_keys=True,
                              default=str)

        def moved(points):  # plan, from the old system to the new one
            pts = np.column_stack([np.asarray(points, float)[:, :2], np.zeros(len(points))])
            return crs.transform_points(pts, "EPSG:32633", "EPSG:32632")[:, :2]

        before = state(ch)
        preview = heights.survey_crs_changes(ch, "EPSG:32632")
        out = {"preview_changes_nothing": state(ch) == before,
               "preview": {k: preview[k] for k in ("refit", "convert", "rebuild", "kept", "heights_unchanged")}}
        # a system it cannot go into without the grid: refused after the drawings were converted aside
        out["refused"] = refused(lambda: heights.set_survey_crs(ch, "EPSG:7792+3855"))
        out["refused_unchanged"] = state(ch) == before
        old = ch.transform.copy()
        result = heights.set_survey_crs(ch, "EPSG:32632")
        out["result"] = {k: result[k] for k in ("crs", "updated", "rebuild", "kept", "converted", "heights_unchanged")}
        d = ch.meta["drawings"]
        unit = np.asarray(shapely.from_wkt(d["units"][0]["wkt"]).exterior.coords)[:4]
        out["unit"] = float(np.abs(unit - moved(square)).max())
        q, p = d["quote"][0], d["points"][0]
        out["quote"] = [float(np.abs(np.array([q["x"], q["y"]]) - moved([[x0 + 1, y0 + 1]])[0]).max()), q["quota_q"],
                        q["sito_q"]]
        out["point"] = [float(np.abs(np.array([p["x"], p["y"]]) - moved([[x0 + 2, y0 + 2]])[0]).max()), p["z"],
                        p["temp_c"]]
        stone = np.asarray(shapely.from_wkt(ch.meta["stones"]["orthomosaic.tif"][0]).exterior.coords)[:4]
        out["stone"] = float(np.abs(stone - moved(square)).max())
        out["elevation_stone"] = ch.meta["stones"]["prospetto_nord.tif"]
        out["patches"] = [float(np.abs(np.asarray(x["polygon"]) - moved(square)).max())
                          for x in (ch.meta["orthomosaic"]["patches"][0], ch.meta["orthomosaic"]["applied_patches"][0],
                                    ch.meta["texture"]["patches"][0])]
        s = json.loads(sections_file.read_text())[0]
        ends = moved([[x0, y0], [x0 + 10, y0]])
        out["section"] = [float(np.abs(np.array([s["p1"], s["p2"]]) - ends).max()),
                          abs(s["length"] - float(np.hypot(*(ends[1] - ends[0])))), s["svg"]]
        follows = ch.transform @ np.linalg.inv(old)  # the chunk, from its old world to its new one
        r = Region.from_dict(ch.meta["region"])
        scale = np.linalg.norm(follows[:3, 0])
        expected = Region(follows[:3, :3] @ region.center + follows[:3, 3], region.size * scale,
                          follows[:3, :3] / scale)
        out["region"] = float(np.abs(r.corners() - expected.corners()).max())
        b = ch.meta["colour_chart"]["bounds"]
        corners = moved([[x0, y0], [x0 + 1, y0], [x0, y0 + 0.5], [x0 + 1, y0 + 0.5]])
        out["chart"] = float(np.abs(np.array(b) - [*corners.min(axis=0), *corners.max(axis=0)]).max())
        other = doc.add_chunk()  # another chunk of the project in the system of this one: the sections stay with it
        other.crs = ch.crs
        again = heights.survey_crs_changes(ch, "EPSG:32633")
        out["shared_sections"] = [again["convert"]["sections"], [k for k in again["kept"] if k["what"] == "sections"]]
        loose = doc.add_chunk()  # a local frame: what was drawn on it stays where it is
        loose.meta["drawings"] = {"units": [{"wkt": wkt}], "quote": []}
        loose.meta["stones"] = {"orthomosaic.tif": [wkt]}
        out["local"] = heights.survey_crs_changes(loose, "EPSG:32633")
        print(json.dumps(out))
    '''), str(tmp_path))
    assert out["preview_changes_nothing"]
    _says_how_to_get_egm2008(out["refused"])
    assert out["refused_unchanged"]
    result = out["result"]
    assert result["crs"] == "EPSG:32632" and result["updated"] is True and result["heights_unchanged"] is False
    assert result["rebuild"] == ["dem", "orthomosaic"]
    assert result["converted"] == {"markers": 0, "units": 1, "quote": 1, "points": 1, "stones": 1, "sections": 1,
                                   "patches": 1, "texture_patches": 1, "region": 1, "colour_chart": 1}
    kept = {k["what"]: k["names"] for k in result["kept"]}
    assert kept == {"versions": ["dem: DEM del 30/09"], "elevations": ["Prospetto nord"],
                    "multispectral": ["reflectance", "NDVI"], "thermal": ["temperature"], "proxies": ["US1"],
                    "stones": ["prospetto_nord.tif"], "section_drawings": ["Sezione 1"]}
    assert out["preview"] == {"refit": True, "convert": result["converted"], "rebuild": result["rebuild"],
                              "kept": result["kept"], "heights_unchanged": False}
    assert out["unit"] < 1e-6 and out["stone"] < 1e-6 and max(out["patches"]) < 1e-6 and out["chart"] < 1e-6
    assert out["quote"][0] < 1e-6 and out["quote"][1:] == [99.5, "S"]  # 2D to 2D: the heights as they are
    assert out["point"][0] < 1e-6 and out["point"][1:] == [98.25, 20.5]
    assert out["elevation_stone"] == ["POLYGON ((0 0, 1 0, 1 1, 0 0))"]  # in the frame of its elevation: as it was
    assert out["section"][0] < 1e-6 and out["section"][1] < 1e-6 and out["section"][2] == "s1.svg"
    assert out["region"] < 1e-6
    assert out["shared_sections"] == [0, [{"what": "sections", "names": ["Sezione 1"]}]]
    local = out["local"]
    assert local["refit"] is False and not any(local["convert"].values())
    assert local["kept"] == [{"what": "stones", "names": ["orthomosaic.tif"]},
                             {"what": "sections", "names": ["Sezione 1"]}, {"what": "drawn", "names": ["units"]}]


def test_set_survey_crs_says_when_heights_pass_as_they_are(tmp_path):
    """From a system without heights of its own into one with EGM2008 heights, a chunk placed by the heights above the
    sea level of its photos (the EXIF) keeps them: the georeference, the markers and what was drawn - spot heights,
    measuring points, units with heights - pass as they are, right only if they were heights above the sea level, and
    the result says so."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        write_grid(value=40.0)
        ch = surveyed(system="EPSG:4326", height="exif")
        marker = ch.add_marker("P1")
        marker.reference = Reference(location=list(utm[0] + [1.0, 2.0, -2.0]), accuracy=[0.01] * 3)
        x0, y0, _ = utm.mean(axis=0)
        unit = shapely.Polygon([(x0, y0, 99.0), (x0 + 2, y0, 99.2), (x0 + 2, y0 + 2, 99.4)]).wkt
        ch.meta["drawings"] = {"units": [{"wkt": unit}], "quote": [{"x": x0, "y": y0, "quota_q": 99.5}],
                               "points": [{"x": x0 + 1, "y": y0, "z": 98.25}]}
        preview = heights.survey_crs_changes(ch, "EPSG:32633+3855")
        result = heights.set_survey_crs(ch, "EPSG:32633+3855")
        drawn = shapely.from_wkt(ch.meta["drawings"]["units"][0]["wkt"])
        print(json.dumps({"preview": [preview["heights_unchanged"], preview["heights_from_ellipsoid"]],
                          "result": [result["heights_unchanged"], result["heights_from_ellipsoid"], result["updated"]],
                          "cameras": [float(ch.to_world(c.center)[2]) for c in ch.cameras],
                          "marker": marker.reference.location[2], "quote": ch.meta["drawings"]["quote"][0]["quota_q"],
                          "point": ch.meta["drawings"]["points"][0]["z"],
                          "unit": [drawn.has_z, [c[2] for c in drawn.exterior.coords[:3]]],
                          "converted": result["converted"]}))
    '''))
    assert out["preview"] == [True, False] and out["result"] == [True, False, True]
    assert out["cameras"] == pytest.approx([100.0, 101.0, 102.0, 100.5], abs=0.01)  # as they were
    assert out["marker"] == pytest.approx(98.0) and out["quote"] == 99.5 and out["point"] == 98.25
    assert out["unit"][0] is True and out["unit"][1] == pytest.approx([99.0, 99.2, 99.4], abs=1e-9)
    assert out["converted"] == {"markers": 1, "units": 1, "quote": 1, "points": 1, "stones": 0, "sections": 0,
                                "patches": 0, "texture_patches": 0, "region": 0, "colour_chart": 0}


def test_set_survey_crs_takes_the_heights_of_a_chunk_placed_by_gnss(tmp_path):
    """A chunk in a system without heights of its own placed by GNSS positions of its photos (an MRK: heights above the
    ellipsoid) has heights above the ellipsoid: into a system with EGM2008 heights its georeference is fitted again
    40 m lower (a geoid of 40 m) and what was drawn on it goes with it - spot heights, measuring points, units with
    heights, 40 m lower, still on the model -, while the heights of the markers, typed, pass as they are. Without the
    grid of the geoid it is refused and nothing changes."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        ch = surveyed()
        marker = ch.add_marker("P1")
        marker.reference = Reference(location=list(utm[0] + [1.0, 2.0, -2.0]), accuracy=[0.01] * 3)
        x0, y0, _ = utm.mean(axis=0)
        unit = shapely.Polygon([(x0, y0, 99.0), (x0 + 2, y0, 99.2), (x0 + 2, y0 + 2, 99.4)]).wkt
        ch.meta["drawings"] = {"units": [{"wkt": unit}], "quote": [{"x": x0 + 1, "y": y0 + 1, "quota_q": 99.5}],
                               "points": [{"x": x0 + 2, "y": y0 + 2, "z": 98.25}]}
        before = json.dumps([ch.crs, ch.transform.tolist(), ch.meta], sort_keys=True)
        out = {"refused": refused(lambda: heights.set_survey_crs(ch, "EPSG:32633+3855"))}
        out["unchanged"] = json.dumps([ch.crs, ch.transform.tolist(), ch.meta], sort_keys=True) == before
        write_grid(value=40.0)
        inside = ch.to_internal(np.array([[x0 + 1, y0 + 1, 99.5]]))[0]  # the point of the model under the spot height
        preview = heights.survey_crs_changes(ch, "EPSG:32633+3855")
        result = heights.set_survey_crs(ch, "EPSG:32633+3855")
        d = ch.meta["drawings"]
        q, drawn = d["quote"][0], shapely.from_wkt(d["units"][0]["wkt"])
        out.update({"preview": [preview["heights_unchanged"], preview["heights_from_ellipsoid"]],
                    "result": [result["heights_unchanged"], result["heights_from_ellipsoid"], result["updated"]],
                    "cameras": [float(ch.to_world(c.center)[2]) for c in ch.cameras],
                    "quote": [q["x"] - (x0 + 1), q["y"] - (y0 + 1), q["quota_q"]], "point": d["points"][0]["z"],
                    "unit": [drawn.has_z, [c[2] for c in drawn.exterior.coords[:3]]],
                    "marker": marker.reference.location[2],
                    "on_the_model": float(np.abs(ch.to_world(inside) - [q["x"], q["y"], q["quota_q"]]).max())})
        print(json.dumps(out))
    '''))
    _says_how_to_get_egm2008(out["refused"])
    assert out["unchanged"]
    assert out["preview"] == [False, True] and out["result"] == [False, True, True]
    assert out["cameras"] == pytest.approx([60.0, 61.0, 62.0, 60.5], abs=0.01)  # above the geoid
    assert out["quote"] == pytest.approx([0.0, 0.0, 59.5], abs=1e-6) and out["point"] == pytest.approx(58.25, abs=1e-6)
    assert out["unit"][0] is True and out["unit"][1] == pytest.approx([59.0, 59.2, 59.4], abs=1e-6)
    assert out["marker"] == pytest.approx(98.0)  # typed: as it was
    assert out["on_the_model"] < 0.001


def test_set_survey_crs_drawn_heights_follow_what_the_fit_converts(tmp_path):
    """The heights drawn on a chunk in a system without heights of its own follow what fitting the georeference again
    does to the heights of the chunk, decided on the system each position of the photos is read in, not on how it is
    marked: from a system with heights above the ellipsoid (EPSG:4979, also marked "exif") they go onto the geoid with
    the model; from one without heights of its own (EPSG:32633, EPSG:4326, also marked "ellipsoidal") they pass as
    they are, as the model's do; from one with heights above the geoid they stay with the model too. Positions read in
    systems of both kinds, or with control markers placing the chunk with them, are refused, nothing changed."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        write_grid(value=40.0)
        x0, y0, _ = utm.mean(axis=0)
        out = {}
        for system, height in (("EPSG:32633", "ellipsoidal"), ("EPSG:4326", "ellipsoidal"),
                               ("EPSG:4979", "ellipsoidal"), ("EPSG:4979", "exif"), ("EPSG:4326", "exif"),
                               ("EPSG:32633+3855", "exif")):
            ch = surveyed(system=system, height=height)
            ch.meta["drawings"] = {"units": [], "quote": [{"x": x0 + 1, "y": y0 + 1, "quota_q": 99.5}]}
            inside = ch.to_internal(np.array([[x0 + 1, y0 + 1, 99.5]]))[0]  # the model under the spot height
            preview = heights.survey_crs_changes(ch, "EPSG:32633+3855")
            heights.set_survey_crs(ch, "EPSG:32633+3855")
            out[f"{system} {height}"] = [float(ch.to_world(inside)[2]), ch.meta["drawings"]["quote"][0]["quota_q"],
                                         preview["heights_unchanged"], preview["heights_from_ellipsoid"]]

        def state(c):
            return json.dumps([c.crs, c.transform.tolist(), c.meta, [m.reference.to_dict() for m in c.markers],
                               [cam.meta for cam in c.cameras]], sort_keys=True, default=str)

        both = surveyed()  # two positions with heights above the ellipsoid, two without heights of their own
        for cam in both.cameras[2:]:
            gnss_apply.set_camera_position(cam, places[cam.key], "EPSG:4326", [0.02, 0.02, 0.04], "exif",
                                           status="fix")
        with_marker = surveyed()  # a control marker placing the chunk with the positions of the photos
        marker = with_marker.add_marker("P1")
        marker.reference = Reference(location=list(utm[0] + [1.0, 2.0, -2.0]), accuracy=[0.01] * 3)
        marker.position = with_marker.to_internal((utm[0] + [1.0, 2.0, -2.0])[None])[0]
        for name, ch in (("both", both), ("with_marker", with_marker)):
            before = state(ch)
            out[name] = [refused(lambda: heights.survey_crs_changes(ch, "EPSG:32633+3855")),
                         refused(lambda: heights.set_survey_crs(ch, "EPSG:32633+3855")), state(ch) == before]
        print(json.dumps(out))
    '''))
    expected = {"EPSG:32633 ellipsoidal": (99.5, [True, False]), "EPSG:4326 ellipsoidal": (99.5, [True, False]),
                "EPSG:4979 ellipsoidal": (59.5, [False, True]), "EPSG:4979 exif": (59.5, [False, True]),
                "EPSG:4326 exif": (99.5, [True, False]), "EPSG:32633+3855 exif": (99.5, [False, False])}
    for row, (height, flags) in expected.items():
        model, quote, *said = out[row]
        assert model == pytest.approx(height, abs=0.01) and quote == pytest.approx(height, abs=1e-6), row
        assert abs(quote - model) < 0.001 and said == flags, row  # the spot height on the model
    for name in ("both", "with_marker"):
        preview, applied, unchanged = out[name]
        assert preview is not None and preview == applied and unchanged, name
        assert "different kinds" in preview and "Switch off" in preview, name
    assert "EPSG:4979" in out["both"][0] and "EPSG:4326" in out["both"][0]
    assert "control marker" in out["with_marker"][0]


def test_set_survey_crs_does_not_promise_a_refit_on_markers_in_a_line(tmp_path):
    """Three control markers in a line cannot fit a georeference again (as update_transform finds): the preview says
    the georeference is converted, as the change then does."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        ch = surveyed()
        for cam in ch.cameras:
            cam.reference = Reference()
        for i in range(3):
            m = ch.add_marker(f"P{i}")
            world = utm.mean(axis=0) + [10.0 * i, 0.0, 0.0]
            m.reference = Reference(location=list(world), accuracy=[0.01] * 3)
            m.position = ch.to_internal(world[None])[0]
        preview = heights.survey_crs_changes(ch, "EPSG:32632")
        result = heights.set_survey_crs(ch, "EPSG:32632")
        print(json.dumps({"preview": [preview["refit"], preview["transform_rms"]],
                          "result": [result["updated"], result["transform_rms"]]}))
    '''))
    refit, rms = out["preview"]
    assert refit is False and rms is not None and rms < 0.001
    assert out["result"] == [False, rms]


def test_set_survey_crs_converts_the_georeference_of_a_chunk_without_photos(tmp_path):
    """A chunk georeferenced without photos to fit it again - a scan, a model or a cloud imported, a satellite chunk -
    has its georeference converted: a similarity fitted on its extent (its region, else what lives in its internal
    frame) from the old system to the new one, so that the model stays with what was drawn on it and the region with
    both, within millimetres; how far the similarity misses is said before and after. Into another zone and into
    RDN2008; a site too large for one similarity is refused and nothing changes."""
    out = _run(tmp_path, textwrap.dedent('''
        import shapely
        import pyarchinit_photogrammetry as ppg
        from pyarchinit_photogrammetry.core import geometry
        from pyarchinit_photogrammetry.core.pointcloud import PointCloud
        from pyarchinit_photogrammetry.engine.region import Region
        from pyarchinit_photogrammetry.io.mesh_ply import write_mesh_ply

        reads = []
        sample = PointCloud.sample_xyz

        def counted(path, *args, **kwargs):  # the clouds and models read for the extent of a chunk
            reads.append(Path(path).name)
            return sample(path, *args, **kwargs)

        PointCloud.sample_xyz = staticmethod(counted)

        folder = Path(sys.argv[1])
        gx, gy = np.meshgrid(np.linspace(0.0, 40.0, 21), np.linspace(0.0, 30.0, 16))
        vertices = np.column_stack([gx.ravel(), gy.ravel(), (3.0 * np.sin(gx / 7.0) + 0.1 * gy).ravel()])
        grid = np.arange(len(vertices)).reshape(gx.shape)
        a, b, c, d = (grid[:-1, :-1].ravel(), grid[:-1, 1:].ravel(), grid[1:, :-1].ravel(), grid[1:, 1:].ravel())
        faces = np.vstack([np.column_stack([a, b, d]), np.column_stack([a, d, c])])
        turn = np.radians(20.0)
        rot = np.array([[np.cos(turn), -np.sin(turn), 0.0], [np.sin(turn), np.cos(turn), 0.0], [0.0, 0.0, 1.0]])

        def scanned(name, region=None):
            """A chunk as a scan or an imported model leaves it: a model in its internal frame, a georeference of its
            own (a round origin, turned), no photos; a unit, a unit with heights, a spot height and a measuring point
            drawn on the model."""
            doc = ppg.Document()
            ch = doc.add_chunk(name)
            doc.save(folder / f"{name}.ppg")
            ch.crs = "EPSG:32633"
            ch.transform = geometry.compose_similarity(1.0, rot, np.array([292000.0, 4642000.0, 40.0]))
            ch._set_product("model", write_mesh_ply(ch.data_dir / "model.ply", vertices, faces))
            world = ch.to_world(vertices)
            ch.meta["drawings"] = {
                "units": [{"wkt": shapely.Polygon(world[[20, 40, 60, 80], :2]).wkt},
                          {"wkt": shapely.Polygon(world[[100, 120, 150]]).wkt}],
                "quote": [{"x": world[200, 0], "y": world[200, 1], "quota_q": world[200, 2]}],
                "points": [{"x": world[250, 0], "y": world[250, 1], "z": world[250, 2]}]}
            if region is not None:
                ch.meta["region"] = Region(world.mean(axis=0), region, rot).to_dict()
            doc.save()
            return ch

        def state(ch):
            return json.dumps([ch.crs, ch.transform.tolist(), ch.meta, ch.products], sort_keys=True, default=str)

        def apart(ch):
            """How far what was drawn is from the model it was drawn on, now (metres)."""
            world = ch.to_world(vertices)
            d = ch.meta["drawings"]
            flat = np.asarray(shapely.from_wkt(d["units"][0]["wkt"]).exterior.coords)[:4]
            tall = np.asarray(shapely.from_wkt(d["units"][1]["wkt"]).exterior.coords)[:3]
            q, p = d["quote"][0], d["points"][0]
            return max(float(np.abs(flat - world[[20, 40, 60, 80], :2]).max()),
                       float(np.abs(tall - world[[100, 120, 150]]).max()),
                       float(np.abs(np.array([q["x"], q["y"], q["quota_q"]]) - world[200]).max()),
                       float(np.abs(np.array([p["x"], p["y"], p["z"]]) - world[250]).max()))

        out = {}
        for name, system, region in (("zone", "EPSG:32632", None), ("rdn2008", "EPSG:7792", [50.0, 40.0, 12.0])):
            ch = scanned(name, region)
            corners = None if region is None else Region.from_dict(ch.meta["region"]).corners()
            before = state(ch)
            preview = heights.survey_crs_changes(ch, system)
            unchanged = state(ch) == before
            result = heights.set_survey_crs(ch, system)
            out[name] = {"preview": [preview["refit"], preview["transform_rms"], preview["convert"]["region"]],
                         "unchanged": unchanged,
                         "result": [result["updated"], result["transform_rms"], result["kept"], ch.crs],
                         "apart": apart(ch), "relief": [result["transform_relief"], result["transform_vertical"]]}
            if corners is not None:  # the region where the conversion takes its corners
                now = Region.from_dict(ch.meta["region"]).corners()
                out[name]["region"] = float(np.abs(now - crs.transform_points(corners, "EPSG:32633", system)).max())
        # a dense cloud with one point 5 km to the side: the extent of the chunk does not follow it
        stray = scanned("stray")
        far = stray.to_internal(np.array([[297000.0, 4642000.0, 40.0]]))
        stray.point_cloud = PointCloud(np.vstack([vertices, far]))
        reads.clear()
        preview = heights.survey_crs_changes(stray, "EPSG:32632")
        result = heights.set_survey_crs(stray, "EPSG:32632")
        out["stray"] = {"rms": [preview["transform_rms"], result["transform_rms"]], "apart": apart(stray),
                        "relief": result["transform_relief"], "reads": sorted(reads)}
        # 200 m sites of 25 and 50 m of relief at Rome, into the next zone: the heights of the model take the change
        # of the scale of the plan over the relief (+500 ppm), said; the plan is converted within a millimetre
        for relief in (25.0, 50.0):
            hill = scanned(f"hill{relief:.0f}", [200.0, 200.0, relief])
            result = heights.set_survey_crs(hill, "EPSG:32632")
            out[f"hill{relief:.0f}"] = [result["transform_rms"], result["transform_relief"],
                                        result["transform_vertical"]]
        huge = scanned("huge", [60000.0, 60000.0, 50.0])  # a region of 60 km: no one similarity into another zone
        before = state(huge)
        out["huge"] = [refused(lambda: heights.survey_crs_changes(huge, "EPSG:32632")),
                       refused(lambda: heights.set_survey_crs(huge, "EPSG:32632")), state(huge) == before]
        print(json.dumps(out))
    '''), str(tmp_path))
    for name in ("zone", "rdn2008"):
        got = out[name]
        refit, rms, region = got["preview"]
        assert refit is False and rms < 0.001 and got["unchanged"], name  # in plan
        assert got["result"] == [False, rms, [], "EPSG:32632" if name == "zone" else "EPSG:7792"], name
        assert got["apart"] < 0.003, name  # the model and what was drawn on it: together
        assert region == (name == "rdn2008")
    assert out["rdn2008"]["region"] < 0.003
    relief, vertical = out["zone"]["relief"]  # the relief of the model, and what the scale of the plan does to it
    assert 5.0 < relief < 9.0 and 0.0004 < vertical / relief < 0.0006
    assert out["rdn2008"]["relief"][1] < 0.0001  # the same projection: nothing
    stray = out["stray"]
    assert stray["rms"][0] == stray["rms"][1] < 0.001 and stray["apart"] < 0.003 and stray["relief"] < 9.0
    assert stray["reads"] == ["model.ply", "point_cloud.ply"]  # read once: the change takes what the preview read
    for relief in (25, 50):
        rms, seen, vertical = out[f"hill{relief}"]
        assert rms < 0.001 and seen == pytest.approx(relief) and vertical == pytest.approx(0.000504 * relief, rel=0.1)
    preview, applied, unchanged = out["huge"]
    assert "similarity" in preview and "in plan" in preview and "m across" in preview
    assert preview == applied and unchanged
