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
    # close_fds=False: started by posix_spawn, as the program starts its own processes - on macOS a child forked
    # after a test has opened a PyArchInit database (SpatiaLite and its PROJ) may die before it runs (tests/test_env.py)
    run = subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, timeout=300,
                         env=environment, close_fds=False)
    tail = (f"exit status {run.returncode}\n--- the last lines of its output:\n{run.stdout[-2000:]}\n"
            f"--- the last lines of its errors:\n{run.stderr[-3000:]}")
    assert run.returncode == 0, tail
    # what it printed last as JSON: a library may write a line of its own after it (a warning at exit)
    for line in reversed(run.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    pytest.fail(f"the script printed no line of JSON; {tail}", pytrace=False)


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


def test_heights_above_one_geoid_into_another_need_both_grids(tmp_path):
    """Heights above EGM96 (EPSG:5773) into heights above EGM2008 (EPSG:3855), or above Genova 1942 (ITALGEO2005,
    EPSG:5214): without the grids they are refused, naming a grid, never passed as they are (the difference of two
    geoids is metres); with them, converted through the heights above the ellipsoid."""
    out = _run(tmp_path, '''
        utm = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633")[0]
        point = [[utm[0], utm[1], 100.0]]
        out = {"egm96": None, "italgeo": None}
        try:
            crs.transform_points(point, "EPSG:32633+5773", "EPSG:32633+3855")
        except heights.GridMissing as exc:
            out["egm96"] = [str(exc), list(exc.grids)]
        try:
            crs.transform_points(point, "EPSG:7792+5214", "EPSG:7792+3855")
        except heights.GridMissing as exc:
            out["italgeo"] = [str(exc), list(exc.grids)]
        # both grids: EGM96 52 m above the ellipsoid, EGM2008 50 m - a height of 100 m above EGM96 is 102 m above
        # EGM2008 (152 m above the ellipsoid)
        write_grid(value=50.0)
        write_grid(name="us_nga_egm96_15.tif", value=52.0)
        heights.refresh()
        out["converted"] = crs.transform_points(point, "EPSG:32633+5773", "EPSG:32633+3855")[0].tolist()
        out["same"] = crs.transform_points(point, "EPSG:32633+3855", "EPSG:32633+3855")[0].tolist()
        print(json.dumps(out))
    ''')
    message, grids = out["egm96"]
    assert grids and "cannot become heights above" in message and "EGM96" in message, message
    message, grids = out["italgeo"]
    assert grids[0] == "us_nga_egm08_25.tif" and "geo_igm_mar06.grd" in grids, grids
    assert "Genoa 1942" in message and "copy it there as well" in message and "48 m" not in message, message
    assert out["converted"][2] == pytest.approx(102.0, abs=1e-3)
    assert out["same"][2] == 100.0


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
        # without a frame the place decides: a system of ETRF2000 in Europe, WGS 84 / UTM beyond
        by_place = {"rome": (12.4964, 41.9028), "bern": (7.4474, 46.948), "paris": (2.3522, 48.8566),
                    "new_york": (-74.006, 40.7128), "london": (-0.1276, 51.5072), "athens": (23.7275, 37.9838),
                    "rhodes": (28.2225, 36.4341), "valletta": (14.5146, 35.8989), "tunis": (10.1815, 36.8065),
                    "istanbul": (28.9784, 41.0082), "izmir": (27.1428, 38.4237), "nicosia": (33.3823, 35.1856),
                    "aleppo": (37.1612, 36.2021), "kyiv": (30.5234, 50.4501), "lviv": (24.0297, 49.8397),
                    "minsk": (27.5615, 53.9045), "kaliningrad": (20.5072, 54.7104), "vilnius": (25.2797, 54.6872),
                    "chisinau": (28.8638, 47.0105), "reykjavik": (-21.9426, 64.1466), "lampedusa": (12.6, 35.5),
                    "longyearbyen": (15.6356, 78.2232), "santiago": (-70.6693, -33.4489)}
        out["by place"] = {name: heights.survey_crs(lon, lat) for name, (lon, lat) in by_place.items()}
        out["in europe"] = {name: heights.in_europe(lon, lat) for name, (lon, lat) in by_place.items()}
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
    assert out["by place"] == {
        "rome": "EPSG:7792+3855", "bern": "EPSG:25832+3855", "paris": "EPSG:25831+3855", "new_york": "EPSG:32618+3855",
        "london": "EPSG:25830+3855", "athens": "EPSG:25834+3855", "rhodes": "EPSG:25835+3855",
        "valletta": "EPSG:25833+3855", "tunis": "EPSG:32632+3855", "istanbul": "EPSG:32635+3855",
        "izmir": "EPSG:32635+3855", "nicosia": "EPSG:32636+3855", "aleppo": "EPSG:32637+3855",
        "kyiv": "EPSG:32636+3855", "lviv": "EPSG:32635+3855", "minsk": "EPSG:32635+3855",
        "kaliningrad": "EPSG:32634+3855", "vilnius": "EPSG:25835+3855", "chisinau": "EPSG:25835+3855",
        "reykjavik": "EPSG:32627+3855", "lampedusa": "EPSG:7792+3855", "longyearbyen": "EPSG:25833+3855",
        "santiago": "EPSG:32719+3855"}
    assert {name for name, inside in out["in europe"].items() if inside} == {
        "rome", "bern", "paris", "london", "athens", "rhodes", "valletta", "vilnius", "chisinau", "lampedusa",
        "longyearbyen"}
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
    nothing changes, without the grid of the geoid (four positions in EPSG:4979, one in the system of the chunk); the
    same chunk with no reference left has its georeference converted (a similarity, within a millimetre of the
    conversion); a chunk in a local frame only takes the system; a system in feet, or with heights in feet, is
    refused."""
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
        # a plan in metres with heights in feet (NAVD88 height in US survey feet, in international feet)
        out["feet_heights"] = [refused(lambda: heights.set_survey_crs(ch, system))
                               for system in ("EPSG:32633+6360", "EPSG:32633+8228")]
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
    _says_how_to_get_egm2008(out["refused"])
    assert out["unchanged"]
    assert out["result"] == {"crs": "EPSG:32632", "updated": True, "rebuild": ["dem", "orthomosaic", "dem_precision"]}
    assert out["method"] == "cameras" and out["crs"] == "EPSG:32632"
    assert out["precision"] == [False, False]  # the precision of the old fit dropped
    assert out["pinned"] == "EPSG:32633"
    assert out["cameras"] < 0.01 and out["marker"] < 1e-6
    assert "geographic" in out["geographic"]
    geocentric, vertical, geographic = out["not_projected"]  # only a projected plan: EPSG:4978 was taken
    assert "geocentric" in geocentric and "vertical" in vertical and "geographic" in geographic and out["still"]
    assert "metre" in out["feet"] and "foot" in out["feet"]
    for said in out["feet_heights"]:
        assert said is not None and "heights" in said and "foot" in said and "metre" in said
    bare = out["bare"]  # its georeference converted: the cameras where the conversion takes them, within a millimetre
    assert bare["preview"][0] is False and bare["preview"][1] < 0.001
    assert bare["result"] == [False, bare["preview"][1], "EPSG:32632"] and bare["cameras"] < 0.001
    assert out["horizontal_kept"]
    assert out["loose"] == {"crs": "EPSG:7792+3855", "updated": False, "rebuild": []}


# a chunk aligned and placed by the GNSS positions of its photos (an MRK), in EPSG:32633 (or ``old``): what a survey is
# before the system is changed; ``utm`` the positions of the photos in WGS 84 / UTM 33N
_SURVEYED = '''
import copy
import shapely
import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.camera import Camera, Reference
from pyarchinit_photogrammetry.engine import dem as dem_mod
from pyarchinit_photogrammetry.engine.region import Region
from pyarchinit_photogrammetry.gnss import apply as gnss_apply

places = [[12.4964 + dx, 41.9028 + dy, 100.0 + dz] for dx, dy, dz in
          [(0, 0, 0), (0.001, 0, 1), (0, 0.001, 2), (0.001, 0.001, 0.5)]]
utm = crs.transform_points(places, "EPSG:4979", "EPSG:32633")


def position(cam, system="EPSG:4979", height="ellipsoidal", legacy=False, **kwargs):
    """The position of the photo ``cam`` at its place in ``system``: longitude, latitude and height for a geographic
    system (in ITRF2020 at the ``epoch`` of ``kwargs``, or with ``legacy`` as a project of before the frames wrote it,
    without frame and epoch), else the numbers of its place in WGS 84 / UTM 33N with its height, as a positions file
    gives them."""
    place, at = places[cam.key % len(places)], utm[cam.key % len(places)]
    location = list(place) if crs.is_geographic(system) else list(at)
    if legacy:
        cam.reference = Reference(location=location, accuracy=[0.02, 0.02, 0.04], enabled=True)
        cam.meta["reference_crs"] = system
        cam.meta["gnss"] = {"source": "ppk", "status": "fix", "height": height}
        return
    gnss_apply.set_camera_position(cam, location, system, [0.02, 0.02, 0.04],
                                   "mrk" if height == "ellipsoidal" else "exif", status="fix", height=height, **kwargs)


def surveyed(doc=None, system="EPSG:4979", height="ellipsoidal", old="EPSG:32633", **kwargs):
    ch = (doc or ppg.Document()).add_chunk()
    ch.crs = old
    for key, at in enumerate(utm):
        cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
        position(cam, system, height, **kwargs)
        cam.transform = np.eye(4)
        cam.transform[:3, 3] = at - utm.mean(axis=0)
        ch.cameras.append(cam)
    ch.update_transform()
    return ch


def state(c):
    """All that set_survey_crs may change on the chunk ``c``."""
    return json.dumps([c.crs, c.transform.tolist(), c.meta, [m.reference.to_dict() for m in c.markers],
                       [None if m.position is None else m.position.tolist() for m in c.markers],
                       [cam.meta for cam in c.cameras], c.products], sort_keys=True, default=str)


# what the preview says and what the change did, the same (survey_crs_changes, set_survey_crs)
SAME = {"refit": "updated", "method": "method", "convert": "converted", "rebuild": "rebuild", "kept": "kept",
        "drawings": "drawings", "drawings_shift": "drawings_shift", "drawings_lift": "drawings_lift",
        "heights_unchanged": "heights_unchanged", "transform_rms": "transform_rms",
        "transform_relief": "transform_relief", "transform_vertical": "transform_vertical"}


def differ(preview, result):
    """The keys on which the preview and the result of a change disagree."""
    return [key for key, said in SAME.items() if preview[key] != result[said]]
'''


def test_set_survey_crs_converts_what_was_drawn_on_the_chunk(tmp_path):
    """Into another system goes also what was drawn on the products of the chunk - the units, the spot heights, the
    measuring points, the stones of the orthomosaic, the sections of the project, the patches of the orthomosaic and
    of the texture, the colour chart - and the region of the reconstruction: by the change of the georeference, so that
    it stays on the model (a drawing of the plan at the height of the ground there); a chunk with nothing its
    georeference places has what was drawn on it converted as coordinates, as its markers. What cannot be converted
    (versions, elevations, rasters of other products, proxies, the drawings of the sections) is listed as kept in the
    old system. What would change can be asked before (nothing changes then, and it is what the change does), and a
    refusal leaves everything as it was, the file of the sections too."""
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
        ch.products.update({"orthomosaic": "orthomosaic.tif", "dem": "dem.tif"})  # files not there: no DEM to read
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

        def saved(c):
            return json.dumps([c.crs, c.transform.tolist(), c.meta, sections_file.read_text()], sort_keys=True,
                              default=str)

        def moved(points):  # plan, from the old system to the new one, by the conversion of the two systems
            pts = np.column_stack([np.asarray(points, float)[:, :2], np.zeros(len(points))])
            return crs.transform_points(pts, "EPSG:32633", "EPSG:32632")[:, :2]

        before = saved(ch)
        preview = heights.survey_crs_changes(ch, "EPSG:32632")
        out = {"preview_changes_nothing": saved(ch) == before}
        # a system the positions of the photos cannot go into without the grid of the geoid: refused, nothing changed
        out["refused"] = refused(lambda: heights.set_survey_crs(ch, "EPSG:7792+3855"))
        out["refused_unchanged"] = saved(ch) == before
        old = ch.transform.copy()

        def follows(points, z):  # where the model that was under ``points`` (at the height ``z``) is now
            pts = np.column_stack([np.asarray(points, float)[:, :2], np.broadcast_to(z, (len(points),))])
            return ch.to_world(geometry.apply(np.linalg.inv(old), pts))

        result = heights.set_survey_crs(ch, "EPSG:32632")
        out["differ"] = differ(preview, result)
        out["result"] = {k: result[k] for k in ("crs", "updated", "method", "rebuild", "kept", "converted",
                                                "heights_unchanged", "drawings")}
        out["said"] = [result["drawings_shift"], result["drawings_lift"]]
        d = ch.meta["drawings"]
        unit = np.asarray(shapely.from_wkt(d["units"][0]["wkt"]).exterior.coords)[:4]
        out["unit"] = float(np.abs(unit - follows(square, 99.5)[:, :2]).max())
        q, p = d["quote"][0], d["points"][0]
        out["quote"] = [float(np.abs(np.array([q["x"], q["y"], q["quota_q"]])
                                     - follows([[x0 + 1, y0 + 1]], 99.5)[0]).max()), q["sito_q"]]
        out["point"] = [float(np.abs(np.array([p["x"], p["y"], p["z"]])
                                     - follows([[x0 + 2, y0 + 2]], 98.25)[0]).max()), p["temp_c"]]
        out["plan"] = float(np.abs(unit - moved(square)).max())  # from the conversion of the coordinates: a hair
        stone = np.asarray(shapely.from_wkt(ch.meta["stones"]["orthomosaic.tif"][0]).exterior.coords)[:4]
        out["stone"] = float(np.abs(stone - follows(square, 99.5)[:, :2]).max())
        out["elevation_stone"] = ch.meta["stones"]["prospetto_nord.tif"]
        out["patches"] = [float(np.abs(np.asarray(x["polygon"]) - follows(square, 99.5)[:, :2]).max())
                          for x in (ch.meta["orthomosaic"]["patches"][0], ch.meta["orthomosaic"]["applied_patches"][0],
                                    ch.meta["texture"]["patches"][0])]
        s = json.loads(sections_file.read_text())[0]
        ends = follows([[x0, y0], [x0 + 10, y0]], 99.5)[:, :2]
        out["section"] = [float(np.abs(np.array([s["p1"], s["p2"]]) - ends).max()),
                          abs(s["length"] - float(np.hypot(*(ends[1] - ends[0])))), s["svg"]]
        change = ch.transform @ np.linalg.inv(old)  # the chunk, from its old world to its new one
        r = Region.from_dict(ch.meta["region"])
        scale = np.linalg.norm(change[:3, 0])
        expected = Region(change[:3, :3] @ region.center + change[:3, 3], region.size * scale,
                          change[:3, :3] / scale)
        out["region"] = float(np.abs(r.corners() - expected.corners()).max())
        b = ch.meta["colour_chart"]["bounds"]
        corners = follows([[x0, y0], [x0 + 1, y0], [x0, y0 + 0.5], [x0 + 1, y0 + 0.5]], 99.5)[:, :2]
        out["chart"] = float(np.abs(np.array(b) - [*corners.min(axis=0), *corners.max(axis=0)]).max())
        other = doc.add_chunk()  # another chunk of the project in the system of this one: the sections stay with it
        other.crs = ch.crs
        again = heights.survey_crs_changes(ch, "EPSG:32633")
        out["shared_sections"] = [again["convert"]["sections"], [k for k in again["kept"] if k["what"] == "sections"]]
        # georeferenced, with nothing in its internal frame (no model, cloud, photo or marker placed) and something
        # drawn: converted as coordinates, the heights as the conversion of the two systems gives them (a project of
        # its own: the sections above are not its)
        empty = ppg.Document().add_chunk()
        empty.crs = "EPSG:32633"
        empty.meta["drawings"] = {"units": [{"wkt": wkt}], "quote": [{"x": x0 + 1, "y": y0 + 1, "quota_q": 99.5}]}
        said = heights.survey_crs_changes(empty, "EPSG:32632")
        done = heights.set_survey_crs(empty, "EPSG:32632")
        q = empty.meta["drawings"]["quote"][0]
        out["empty"] = {"differ": differ(said, done), "drawings": done["drawings"], "updated": done["updated"],
                        "said": [done["drawings_shift"], done["drawings_lift"], done["transform_rms"]],
                        "unit": float(np.abs(np.asarray(shapely.from_wkt(empty.meta["drawings"]["units"][0]["wkt"])
                                                        .exterior.coords)[:4] - moved(square)).max()),
                        "quote": [float(np.abs(np.array([q["x"], q["y"]]) - moved([[x0 + 1, y0 + 1]])[0]).max()),
                                  q["quota_q"]]}
        loose = doc.add_chunk()  # a local frame: what was drawn on it stays where it is
        loose.meta["drawings"] = {"units": [{"wkt": wkt}], "quote": []}
        loose.meta["stones"] = {"orthomosaic.tif": [wkt]}
        out["local"] = heights.survey_crs_changes(loose, "EPSG:32633")
        print(json.dumps(out))
    '''), str(tmp_path))
    assert out["preview_changes_nothing"]
    _says_how_to_get_egm2008(out["refused"])
    assert out["refused_unchanged"]
    assert out["differ"] == []  # the preview said what the change did
    result = out["result"]
    assert result["crs"] == "EPSG:32632" and result["updated"] is True and result["method"] == "cameras"
    assert result["heights_unchanged"] is False and result["drawings"] == "follow"
    assert result["rebuild"] == ["dem", "orthomosaic"]
    assert result["converted"] == {"markers": 0, "units": 1, "quote": 1, "points": 1, "stones": 1, "sections": 1,
                                   "patches": 1, "texture_patches": 1, "region": 1, "colour_chart": 1}
    kept = {k["what"]: k["names"] for k in result["kept"]}
    assert kept == {"versions": ["dem: DEM del 30/09"], "elevations": ["Prospetto nord"],
                    "multispectral": ["reflectance", "NDVI"], "thermal": ["temperature"], "proxies": ["US1"],
                    "stones": ["prospetto_nord.tif"], "section_drawings": ["Sezione 1"]}
    # the next zone scales the plan, and the fit the heights about the photos, by 500 ppm: a few millimetres 6 m below
    shift, (low, high) = out["said"]
    assert shift < 0.001 and -0.005 < low <= high < 0.005
    # on the model, within a tenth of a millimetre: in plan, and with their heights
    assert out["unit"] < 1e-4 and out["stone"] < 1e-4 and max(out["patches"]) < 1e-4 and out["chart"] < 1e-4
    assert out["quote"][0] < 1e-6 and out["quote"][1] == "S" and out["point"][0] < 1e-6 and out["point"][1] == 20.5
    assert out["plan"] < 0.001
    assert out["elevation_stone"] == ["POLYGON ((0 0, 1 0, 1 1, 0 0))"]  # in the frame of its elevation: as it was
    assert out["section"][0] < 1e-4 and out["section"][1] < 1e-6 and out["section"][2] == "s1.svg"
    assert out["region"] < 1e-6
    assert out["shared_sections"] == [0, [{"what": "sections", "names": ["Sezione 1"]}]]
    empty = out["empty"]
    assert empty["differ"] == [] and empty["drawings"] == "convert" and empty["updated"] is False
    assert empty["said"] == [None, None, None]
    assert empty["unit"] < 1e-6 and empty["quote"][0] < 1e-6 and empty["quote"][1] == 99.5  # 2D to 2D: as it is
    local = out["local"]
    assert local["refit"] is False and not any(local["convert"].values()) and local["drawings"] == "kept"
    assert local["kept"] == [{"what": "stones", "names": ["orthomosaic.tif"]},
                             {"what": "sections", "names": ["Sezione 1"]}, {"what": "drawn", "names": ["units"]}]


def test_set_survey_crs_says_when_heights_pass_as_they_are(tmp_path):
    """From a system without heights of its own into one with EGM2008 heights, the typed heights of the markers pass as
    they are, right only if they were heights above the sea level, and the result says so; a chunk placed by the
    heights above the sea level of its photos (the EXIF) keeps its heights, and what was drawn on it stays on the
    model - spot heights, measuring points, units with heights as they were."""
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
        print(json.dumps({"differ": differ(preview, result),
                          "result": [result["heights_unchanged"], result["drawings"], result["updated"]],
                          "lift": result["drawings_lift"],
                          "cameras": [float(ch.to_world(c.center)[2]) for c in ch.cameras],
                          "marker": marker.reference.location[2], "quote": ch.meta["drawings"]["quote"][0]["quota_q"],
                          "point": ch.meta["drawings"]["points"][0]["z"],
                          "unit": [drawn.has_z, [c[2] for c in drawn.exterior.coords[:3]]],
                          "converted": result["converted"]}))
    '''))
    assert out["differ"] == [] and out["result"] == [True, "follow", True]
    assert out["cameras"] == pytest.approx([100.0, 101.0, 102.0, 100.5], abs=0.01)  # as they were
    assert out["marker"] == 98.0  # typed: as it was
    assert out["lift"] == pytest.approx([0.0, 0.0], abs=1e-4)  # the model kept its heights: so did the drawings
    assert out["quote"] == pytest.approx(99.5, abs=1e-6) and out["point"] == pytest.approx(98.25, abs=1e-6)
    assert out["unit"][0] is True and out["unit"][1] == pytest.approx([99.0, 99.2, 99.4], abs=1e-6)
    assert out["converted"] == {"markers": 1, "units": 1, "quote": 1, "points": 1, "stones": 0, "sections": 0,
                                "patches": 0, "texture_patches": 0, "region": 0, "colour_chart": 0}


def test_set_survey_crs_takes_the_heights_of_a_chunk_placed_by_gnss(tmp_path):
    """A chunk in a system without heights of its own placed by GNSS positions of its photos (an MRK: heights above the
    ellipsoid) has heights above the ellipsoid: into a system with EGM2008 heights its georeference is fitted again
    40 m lower (a geoid of 40 m) and what was drawn on it goes with it - spot heights, measuring points, units with
    heights, 40 m lower, still on the model, as the preview says -, while the heights of the markers, typed, pass as
    they are. Without the grid of the geoid it is refused and nothing changes."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        ch = surveyed()
        marker = ch.add_marker("P1")
        marker.reference = Reference(location=list(utm[0] + [1.0, 2.0, -2.0]), accuracy=[0.01] * 3)
        x0, y0, _ = utm.mean(axis=0)
        unit = shapely.Polygon([(x0, y0, 99.0), (x0 + 2, y0, 99.2), (x0 + 2, y0 + 2, 99.4)]).wkt
        ch.meta["drawings"] = {"units": [{"wkt": unit}], "quote": [{"x": x0 + 1, "y": y0 + 1, "quota_q": 99.5}],
                               "points": [{"x": x0 + 2, "y": y0 + 2, "z": 98.25}]}
        before = state(ch)
        out = {"refused": [refused(lambda: heights.survey_crs_changes(ch, "EPSG:32633+3855")),
                           refused(lambda: heights.set_survey_crs(ch, "EPSG:32633+3855"))]}
        out["unchanged"] = state(ch) == before
        write_grid(value=40.0)
        inside = ch.to_internal(np.array([[x0 + 1, y0 + 1, 99.5]]))[0]  # the point of the model under the spot height
        preview = heights.survey_crs_changes(ch, "EPSG:32633+3855")
        result = heights.set_survey_crs(ch, "EPSG:32633+3855")
        d = ch.meta["drawings"]
        q, drawn = d["quote"][0], shapely.from_wkt(d["units"][0]["wkt"])
        out.update({"differ": differ(preview, result),
                    "result": [result["heights_unchanged"], result["drawings"], result["updated"], result["method"]],
                    "said": [result["drawings_shift"], result["drawings_lift"]],
                    "cameras": [float(ch.to_world(c.center)[2]) for c in ch.cameras],
                    "quote": [q["x"] - (x0 + 1), q["y"] - (y0 + 1), q["quota_q"]], "point": d["points"][0]["z"],
                    "unit": [drawn.has_z, [c[2] for c in drawn.exterior.coords[:3]]],
                    "marker": marker.reference.location[2],
                    "on_the_model": float(np.abs(ch.to_world(inside) - [q["x"], q["y"], q["quota_q"]]).max())})
        print(json.dumps(out))
    '''))
    for said in out["refused"]:  # the preview and the change alike
        _says_how_to_get_egm2008(said)
    assert out["unchanged"] and out["differ"] == []
    assert out["result"] == [True, "follow", True, "cameras"]  # the marker, typed, passes as it is
    shift, lift = out["said"]
    assert shift < 0.001 and lift == pytest.approx([-40.0, -40.0], abs=1e-4)  # 40 m lower, with the model
    assert out["cameras"] == pytest.approx([60.0, 61.0, 62.0, 60.5], abs=0.01)  # above the geoid
    assert out["quote"] == pytest.approx([0.0, 0.0, 59.5], abs=1e-6) and out["point"] == pytest.approx(58.25, abs=1e-6)
    assert out["unit"][0] is True and out["unit"][1] == pytest.approx([59.0, 59.2, 59.4], abs=1e-6)
    assert out["marker"] == pytest.approx(98.0)  # typed: as it was
    assert out["on_the_model"] < 0.001


def test_the_preview_says_how_markers_and_the_model_part(tmp_path):
    """Photos placed in ITRF2020 at their epoch put the chunk on WGS 84 / UTM; into RDN2008 / UTM its georeference is
    fitted again on them - by the Helmert at their epoch, a metre from the conversion of the old georeference - while
    the typed references of the markers are converted system to system: the preview says how far they come apart from
    the model (and how far they were before), and how far the model itself moves from the conversion of its old
    georeference, though nothing was drawn on it; the change says the same."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        ch = surveyed(system="EPSG:9989", frame="ITRF2020", epoch=2026.82)
        marker = ch.add_marker("P1")  # a check point: it does not take part in the fit
        marker.position, marker.checkpoint = np.array([1.0, 2.0, 0.5]), True
        marker.reference = Reference(location=list(ch.to_world(marker.position)), accuracy=[0.01] * 3)
        preview = heights.survey_crs_changes(ch, "EPSG:7792")
        result = heights.set_survey_crs(ch, "EPSG:7792")
        print(json.dumps({"preview": [preview["markers_apart"], preview["model_shift"], preview["model_lift"],
                                      preview["drawings_shift"]],
                          "result": [result["markers_apart"], result["model_shift"], result["model_lift"]],
                          "refit": [result["updated"], result["method"]]}))
    '''))
    (before, after), shift, lift, drawn = out["preview"]
    assert out["refit"] == [True, "cameras"] and drawn is None  # nothing drawn
    assert before < 0.001 and after == pytest.approx(1.03, abs=0.01)  # the marker a metre from the model
    assert shift == pytest.approx(1.03, abs=0.01) and max(abs(v) for v in lift) < 0.01
    assert out["result"] == out["preview"][:3]


# how much higher the model is, after the change of system, under a spot height drawn on it: what the rows of the
# reviews give with a geoid of 40 m; None where it is no round number (two kinds of height in one fit, the Helmert of
# ITRF2020 at its epoch, PROJ's own Helmert into ETRS89)
LIFTED = {"EPSG:32633 ellipsoidal": 0.0, "EPSG:4326 ellipsoidal": 0.0, "EPSG:4979 ellipsoidal": -40.0,
          "EPSG:4979 exif": -40.0, "EPSG:4326 exif": 0.0, "EPSG:32633+3855 exif": 0.0,
          "itrf2020 EPSG:32633+3855": -40.0, "legacy EPSG:32633+3855": -40.0, "legacy EPSG:7792+3855": -40.0,
          "mrk EPSG:7792+3855": -40.0, "mrk EPSG:25833+3855": -40.0, "mrk EPSG:7791+3855": -40.0,
          "exif EPSG:7792+3855": 0.0, "exif EPSG:25833+3855": 0.0, "exif EPSG:7791+3855": 0.0,
          "not placing": -40.0, "checkpoint": -40.0, "control off": -40.0, "exif + marker": 0.0,
          "r1 EPSG:32633+3855 EPSG:32633": 40.0, "r1 EPSG:32633+3855 EPSG:7792": 40.0,
          "r1 EPSG:7792+3855 EPSG:7792": 40.0, "r2 EPSG:3004 EPSG:32633+3855": -40.0,
          "r2 EPSG:3004 EPSG:7792+3855": -40.0, "r2 EPSG:3004 EPSG:3004+3855": -40.0,
          "r2 EPSG:23033 EPSG:32633+3855": -40.0, "r2 EPSG:23033 EPSG:7792+3855": -40.0,
          "r2 EPSG:23033 EPSG:23033+3855": -40.0, "r2 EPSG:25833": -40.0, "r2 EPSG:7792": -40.0,
          "r2 exif EPSG:3004": 0.0, "stale markers": -40.0}


def test_set_survey_crs_drawings_follow_the_model(tmp_path):
    """What was drawn on a chunk goes into the new system by the change of the georeference itself - the new one after
    the old one undone - so that it stays on the model whatever the references are: positions of the photos read in
    systems with heights above the ellipsoid, without heights of their own, with the heights of one geoid or of two,
    in ITRF2020 at their epoch (into ETRF2000 the Helmert moves the model about a metre: the drawings with it, as the
    preview says) or written before the frames, mixed with each other or with control markers, photos and markers that
    do not place the chunk, markers whose photos the alignment no longer places; from a system with heights above the
    geoid into one without (R1), from a 2D system on another datum (Monte Mario, ED50: R2). For every row the spot
    height, a unit of the plan (at the height of the DEM there) and a unit with heights stay on the model within a
    millimetre, the preview changed nothing and said what the change then did."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        write_grid(value=40.0)
        X = "EPSG:32633+3855"
        rows = {}

        def change(name, ch, new):
            """A spot height, a unit of the plan and one with heights drawn on the model of ch, over a DEM of its
            ground; then ch into new: how far each is from the model (metres), and what was said and done."""
            inside = np.mean([c.center for c in ch.aligned_cameras], axis=0) + [1.0, 1.0, -1.375]
            x, y, z = ch.to_world(inside[None])[0]
            square = np.array([[x - 0.5, y - 0.5], [x + 0.5, y - 0.5], [x + 0.5, y + 0.5], [x - 0.5, y + 0.5]])
            ch.meta["drawings"] = {"units": [{"wkt": shapely.Polygon(square).wkt},
                                             {"wkt": shapely.Polygon(np.column_stack([square, np.full(4, z)])).wkt}],
                                   "quote": [{"x": x, "y": y, "quota_q": z}]}
            dem = dem_mod.write_geotiff(ch.data_dir / "dem.tif", np.full((80, 80), z, np.float32),
                                        (x - 200.0, y + 200.0), 5.0, ch.crs, nodata=dem_mod.NODATA)
            ch._set_product("dem", dem)
            old = ch.transform.copy()
            before = state(ch)
            preview = heights.survey_crs_changes(ch, new)
            unchanged = state(ch) == before
            result = heights.set_survey_crs(ch, new)
            model = ch.to_world(inside[None])[0]
            on = ch.to_world(geometry.apply(np.linalg.inv(old), np.column_stack([square, np.full(4, z)])))
            d = ch.meta["drawings"]
            q = d["quote"][0]
            flat = np.asarray(shapely.from_wkt(d["units"][0]["wkt"]).exterior.coords)[:4]
            tall = np.asarray(shapely.from_wkt(d["units"][1]["wkt"]).exterior.coords)[:4]
            rows[name] = {"quote": float(np.abs(np.array([q["x"], q["y"], q["quota_q"]]) - model).max()),
                          "flat": float(np.abs(flat - on[:, :2]).max()), "tall": float(np.abs(tall - on).max()),
                          "model": float(model[2]), "unchanged": unchanged, "differ": differ(preview, result),
                          "method": result["method"], "drawings": result["drawings"],
                          "shift": result["drawings_shift"], "lift": result["drawings_lift"],
                          "dz": float(model[2] - z)}

        # the rows of the second and third re-reviews: positions in these systems, so marked, into X
        for system, height in (("EPSG:32633", "ellipsoidal"), ("EPSG:4326", "ellipsoidal"),
                               ("EPSG:4979", "ellipsoidal"), ("EPSG:4979", "exif"), ("EPSG:4326", "exif"),
                               ("EPSG:32633+3855", "exif")):
            change(f"{system} {height}", surveyed(system=system, height=height), X)
        # PPK in ITRF2020 at its epoch, and as written before the frames; into WGS 84 and into ETRF2000 (RDN2008,
        # ETRS89)
        for new in (X, "EPSG:7792+3855", "EPSG:25833+3855"):
            change(f"itrf2020 {new}", surveyed(system="EPSG:9989", frame="ITRF2020", epoch=2026.7), new)
            change(f"legacy {new}", surveyed(system="EPSG:9989", legacy=True), new)
        for new in ("EPSG:7792+3855", "EPSG:25833+3855", "EPSG:7791+3855"):  # RDN2008, ETRS89, another zone
            change(f"mrk {new}", surveyed(), new)
            change(f"exif {new}", surveyed(system="EPSG:4326", height="exif"), new)
        # what does not place the chunk: photos switched off, a photo not aligned, a check marker, a marker off
        ch = surveyed()
        for key in (4, 5, 6):
            cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
            position(cam, "EPSG:4326", "exif")
            if key < 6:
                cam.reference.enabled = False
                cam.transform = np.eye(4)
                cam.transform[:3, 3] = utm[key % 4] - utm.mean(axis=0) + [3.0, 3.0, -5.0]
            ch.cameras.append(cam)
        change("not placing", ch, X)
        for name, checkpoint, enabled in (("checkpoint", True, True), ("control off", False, False)):
            ch = surveyed()
            m = ch.add_marker(name)
            m.checkpoint = checkpoint
            world = utm[0] + [3.0, 3.0, -2.0]
            m.reference = Reference(location=list(world), accuracy=[0.01] * 3, enabled=enabled)
            m.position = ch.to_internal(world[None])[0]
            change(name, ch, X)
        # a control marker placing the chunk with the positions of the photos (markers+cameras)
        for name, system, height in (("exif + marker", "EPSG:4326", "exif"), ("mrk + marker", "EPSG:4979",
                                                                              "ellipsoidal")):
            ch = surveyed(system=system, height=height)
            m = ch.add_marker("P1")
            world = utm[0] + [1.0, 2.0, -2.0]
            m.reference = Reference(location=list(world), accuracy=[0.01] * 3)
            m.position = ch.to_internal(world[None])[0]
            change(name, ch, X)
        # positions of two kinds of height, and of two geoids, in one fit
        ch = surveyed()
        for cam in ch.cameras[2:]:
            position(cam, "EPSG:4326", "exif")
        change("mixed", ch, X)
        ch = surveyed(system="EPSG:32633+3855", height="exif")
        for cam in ch.cameras[2:]:
            position(cam, "EPSG:32633+5773", "exif")
        # from one geoid into the other needs the grids of both (EGM96 here 41 m above the ellipsoid)
        write_grid(name="us_nga_egm96_15.tif", value=41.0)
        heights.refresh()
        change("two geoids", ch, X)
        # R1: from heights above the geoid into a system without heights of its own
        for old, new in (("EPSG:32633+3855", "EPSG:32633"), ("EPSG:32633+3855", "EPSG:7792"),
                         ("EPSG:7792+3855", "EPSG:7792")):
            change(f"r1 {old} {new}", surveyed(old=old), new)
        # R2: from a system without heights of its own on another datum (Monte Mario, ED50)
        for old in ("EPSG:3004", "EPSG:23033"):
            for new in ("EPSG:32633+3855", "EPSG:7792+3855", f"{old}+3855"):
                change(f"r2 {old} {new}", surveyed(old=old), new)
        for old in ("EPSG:25833", "EPSG:7792"):
            change(f"r2 {old}", surveyed(old=old), f"{old}+3855")
        change("r2 exif EPSG:3004", surveyed(old="EPSG:3004", system="EPSG:4326", height="exif"), "EPSG:3004+3855")
        # R3: control markers placed once, whose photos are no longer aligned: the alignment does not place them
        ch = surveyed()
        far = Camera(key=4, label="c4", path="c4.jpg", sensor_key=0)
        ch.cameras.append(far)
        for i in range(3):
            m = ch.add_marker(f"S{i}")
            world = utm[i] + [2.0, 1.0, -1.5]
            m.reference = Reference(location=list(world), accuracy=[0.01] * 3)
            m.position = ch.to_internal(world[None])[0]
            ch.add_marker_projection(m, far, 100.0, 120.0)
        change("stale markers", ch, X)
        print(json.dumps(rows))
    '''))
    assert set(LIFTED) <= set(out), set(LIFTED) - set(out)
    for name, row in out.items():
        assert row["unchanged"] and row["differ"] == [], (name, row)  # the preview changed nothing, and said it all
        assert row["drawings"] == "follow" and row["method"] is not None, (name, row)
        assert row["quote"] < 0.001 and row["flat"] < 0.001 and row["tall"] < 0.001, (name, row)  # on the model
        low, high = row["lift"]  # the change of the heights of what was drawn, said before: the model's there
        assert low - 1e-6 <= row["dz"] <= high + 1e-6, (name, row)
        if name in LIFTED:
            assert row["dz"] == pytest.approx(LIFTED[name], abs=0.01), (name, row)
            assert high - low < 0.002, (name, row)  # all of them by as much
    for new in ("EPSG:7792+3855", "EPSG:25833+3855"):  # the Helmert at the epoch, about a metre, said
        assert 0.5 < out[f"itrf2020 {new}"]["shift"] < 1.5, out[f"itrf2020 {new}"]
    assert out["stale markers"]["method"] == "cameras"  # the markers no photo aligned places: not used
    assert out["exif + marker"]["method"] == out["mrk + marker"]["method"] == "markers+cameras"
    assert out["EPSG:4979 ellipsoidal"]["lift"] == pytest.approx([-40.0, -40.0], abs=0.002)
    assert out["r1 EPSG:32633+3855 EPSG:32633"]["lift"] == pytest.approx([40.0, 40.0], abs=0.002)


def test_set_survey_crs_flat_drawings_take_the_height_of_the_ground(tmp_path):
    """A drawing of the plan (a unit without heights, a stone, a patch, a section) follows the model at the height of
    the ground under it: the DEM there, else the median height of the region of the chunk. It matters when the new
    georeference tilts the model: here a geoid that slopes eastwards (1.2 mm a metre) tilts the fit of the photos by
    0.07 degrees, and a point 50 m below the photos would be 6 cm off in plan at their height."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
        write_grid(value=lambda lon, lat: 40.0 + 100.0 * (lon - 12.0))
        ch = surveyed()
        x0, y0, _ = utm.mean(axis=0)
        ground = 50.0  # far below the photos (100-102 m)
        dem = dem_mod.write_geotiff(ch.data_dir / "dem.tif", np.full((60, 60), ground, np.float32),
                                    (x0 - 150.0, y0 + 150.0), 5.0, ch.crs, nodata=dem_mod.NODATA)
        ch._set_product("dem", dem)
        ch.meta["region"] = Region([x0, y0, 70.0], [500.0, 500.0, 60.0], np.eye(3)).to_dict()  # median height 70 m
        on = np.array([[x0 + 10.0, y0 + 10.0], [x0 + 12.0, y0 + 10.0], [x0 + 12.0, y0 + 12.0]])  # on the DEM
        off = on + [190.0, 0.0]  # beyond it
        ch.meta["drawings"] = {"units": [{"wkt": shapely.Polygon(on).wkt}, {"wkt": shapely.Polygon(off).wkt}]}
        old = ch.transform.copy()
        preview = heights.survey_crs_changes(ch, "EPSG:32633+3855")
        result = heights.set_survey_crs(ch, "EPSG:32633+3855")

        def follows(xy, z):
            pts = np.column_stack([xy, np.full(len(xy), z)])
            return ch.to_world(geometry.apply(np.linalg.inv(old), pts))[:, :2]

        units = ch.meta["drawings"]["units"]
        got = [np.asarray(shapely.from_wkt(u["wkt"]).exterior.coords)[:3] for u in units]
        print(json.dumps({"on": [float(np.abs(got[0] - follows(on, z)).max()) for z in (ground, 70.0, 101.0)],
                          "off": [float(np.abs(got[1] - follows(off, z)).max()) for z in (ground, 70.0, 101.0)],
                          "differ": differ(preview, result), "method": result["method"]}))
    '''))
    assert out["differ"] == [] and out["method"] == "cameras"
    on_ground, on_level, on_photos = out["on"]
    assert on_ground < 0.001 and on_level > 0.01 and on_photos > 0.01  # the height of the DEM there
    off_ground, off_level, off_photos = out["off"]
    assert off_level < 0.001 and off_ground > 0.01 and off_photos > 0.01  # beyond the DEM: the median of the region


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
                          "result": [result["updated"], result["transform_rms"]], "differ": differ(preview, result)}))
    '''))
    refit, rms = out["preview"]
    assert refit is False and rms is not None and rms < 0.001
    assert out["result"] == [False, rms] and out["differ"] == []


def test_set_survey_crs_converts_the_georeference_of_a_chunk_without_photos(tmp_path):
    """A chunk georeferenced without photos to fit it again - a scan, a model or a cloud imported, a satellite chunk -
    has its georeference converted: a similarity fitted on its extent (its region, else what lives in its internal
    frame) from the old system to the new one, composed with the georeference; what was drawn on it goes by the same
    similarity, so that it stays on the model within a millimetre, and the region with both; how far the similarity
    misses is said before and after, as the change does. Into another zone and into RDN2008; a site too large for one
    similarity is refused and nothing changes. A cloud is read for its extent once, at most the points asked."""
    out = _run(tmp_path, _SURVEYED + textwrap.dedent('''
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
                         "unchanged": unchanged, "differ": differ(preview, result), "drawings": result["drawings"],
                         "result": [result["updated"], result["transform_rms"], result["kept"], ch.crs],
                         "apart": apart(ch), "relief": [result["transform_relief"], result["transform_vertical"]]}
            if corners is not None:  # the region where the conversion takes its corners
                now = Region.from_dict(ch.meta["region"]).corners()
                out[name]["region"] = float(np.abs(now - crs.transform_points(corners, "EPSG:32633", system)).max())
        model = ppg.Document.open(folder / "zone.ppg").chunk.product_path("model")  # 336 vertices
        out["sampled"] = [len(sample(model, at_most=k)) for k in (100, 200, 335, 336, 1000)]
        # a dense cloud with one point 5 km to the side: the extent of the chunk does not follow it
        stray = scanned("stray")
        far = stray.to_internal(np.array([[297000.0, 4642000.0, 40.0]]))
        stray.point_cloud = PointCloud(np.vstack([vertices, far]))
        reads.clear()
        preview = heights.survey_crs_changes(stray, "EPSG:32632")
        result = heights.set_survey_crs(stray, "EPSG:32632")
        out["stray"] = {"rms": [preview["transform_rms"], result["transform_rms"]], "apart": apart(stray),
                        "differ": differ(preview, result),
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
        assert got["differ"] == [] and got["drawings"] == "follow", name
        assert got["apart"] < 0.001, name  # the model and what was drawn on it: together
        assert region == (name == "rdn2008")
    assert out["rdn2008"]["region"] < 0.003
    relief, vertical = out["zone"]["relief"]  # the relief of the model, and what the scale of the plan does to it
    assert 5.0 < relief < 9.0 and 0.0004 < vertical / relief < 0.0006
    assert out["rdn2008"]["relief"][1] < 0.0001  # the same projection: nothing
    stray = out["stray"]
    assert stray["rms"][0] == stray["rms"][1] < 0.001 and stray["apart"] < 0.001 and stray["relief"] < 9.0
    assert stray["differ"] == []
    for asked, sampled in zip((100, 200, 335, 336, 1000), out["sampled"]):  # at most what is asked, spread evenly
        assert min(asked, 336) / 2 < sampled <= min(asked, 336), (asked, sampled)
    assert stray["reads"] == ["model.ply", "point_cloud.ply"]  # read once: the change takes what the preview read
    for relief in (25, 50):
        rms, seen, vertical = out[f"hill{relief}"]
        assert rms < 0.001 and seen == pytest.approx(relief) and vertical == pytest.approx(0.000504 * relief, rel=0.1)
    preview, applied, unchanged = out["huge"]
    assert "similarity" in preview and "in plan" in preview and "m across" in preview
    assert preview == applied and unchanged
