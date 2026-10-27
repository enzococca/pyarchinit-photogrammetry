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
                  "paris": (2.3522, 48.8566), "new_york": (-74.006, 40.7128), "santiago": (-70.6693, -33.4489)}
        out = {}
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
    assert out["rome WGS84"] == "EPSG:32633+3855" and out["milan WGS84"] == "EPSG:32632+3855"
    assert out["paris ETRF2000"] == "EPSG:25831+3855"  # ETRS89 / UTM 31N outside Italy
    assert out["new_york ETRF2000"] == "EPSG:32618+3855"  # no ETRS89 / UTM there
    assert out["santiago WGS84"] == out["santiago ETRF2000"] == "EPSG:32719+3855"  # south
    assert out["rome plain"] == "EPSG:7792"
    assert out["rome itrf2020"] == "EPSG:32633+3855"  # ITRF2020 is WGS 84 for the program
    assert "NAD83" in out["refused"] and "latitude" in out["off"]


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
    the products built in the old system are named to be built again; a system with EGM2008 heights without the grid
    is refused and nothing changes, as when nothing can fit the georeference again; a chunk not aligned only takes the
    system."""
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
        bare = copy.deepcopy(ch)  # the same chunk without a reference: nothing places it again
        for cam in bare.cameras:
            cam.reference = Reference()
        bare.markers = []
        out = {"fitted": ch.update_transform()["method"]}
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
        before = state(bare)
        out["nothing_fits"] = refused(lambda: heights.set_survey_crs(bare, "EPSG:32632"))
        out["bare_unchanged"] = state(bare) == before
        loose = ppg.Document().add_chunk()  # not aligned: it only takes the system
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
    assert "nothing fits" in out["nothing_fits"] and out["bare_unchanged"]
    assert out["loose"] == {"crs": "EPSG:7792+3855", "updated": False, "rebuild": []}
