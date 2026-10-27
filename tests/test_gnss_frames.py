"""The reference frame and the epoch of every GNSS position: ITRF2020 goes to the European frames (ETRF2000, the frame
of RDN2008 and ETRS89) by the time-dependent Helmert of the EPSG at the epoch of the positions, never by the ballpark
PROJ picks by itself, and to WGS 84 as it is; the positions the program writes carry their frame and epoch, and those
of the projects made before keep converting as they always did."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer
from pyproj.transformer import AreaOfInterest, TransformerGroup

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import crs
from pyarchinit_photogrammetry.core.camera import Camera, Reference
from pyarchinit_photogrammetry.engine import chunks
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import frames, ppk, quality
from pyarchinit_photogrammetry.markers import gcp
from test_gnss import _jpeg, _snapshot  # a photo with its time and position, the references of a chunk
from test_gnss_ppk import PPK_BASE, PPK_NAV, PPK_ROVER, START_2005, _drone_rows, _options, _pos_text, _ppk_flight
from test_gnss_ppk import fake_rtklib  # noqa: F401 - RTKLIB as a script that logs its runs (a fixture)

ROME = [[12.4964, 41.9028, 100.0]]  # longitude, latitude, height above the ellipsoid
FRAMES_LOG = "pyarchinit_photogrammetry.gnss.frames"


# --- the frames and their coordinate systems --------------------------------------------------------------------


def test_the_frames_of_the_coordinate_systems():
    assert frames.FRAMES == {"ITRF2020": "EPSG:9989", "ETRF2000": "EPSG:7931", "ETRF2014": "EPSG:8403",
                             "ETRF2020": "EPSG:10570", "unknown": "EPSG:4979"}
    assert frames.DYNAMIC == frozenset({"ITRF2020"})
    expected = {
        # ITRF2020 in every form, and the realisations of the IGS that are ITRF2020 for the program (IGS20, IGb20)
        "EPSG:9989": "ITRF2020", "EPSG:9988": "ITRF2020", "EPSG:9990": "ITRF2020", "EPSG:10177": "ITRF2020",
        "EPSG:10784": "ITRF2020",
        # ETRF2000, RDN2008 (its realisation in Italy) and ETRS89, also inside a compound system with heights
        "EPSG:7931": "ETRF2000", "EPSG:6706": "ETRF2000", "EPSG:7792": "ETRF2000", "EPSG:7792+3855": "ETRF2000",
        "EPSG:4937": "ETRF2000", "EPSG:25833": "ETRF2000",
        "EPSG:8403": "ETRF2014", "EPSG:10570": "ETRF2020",
        "EPSG:4979": "WGS84", "EPSG:4326": "WGS84", "EPSG:32633": "WGS84",
        # any other datum, a local frame, no system at all
        "EPSG:3004": None, "LOCAL": None, None: None}
    for system, frame in expected.items():
        assert frames.frame_of(system) == frame, system
        if frame is not None:  # every frame frame_of gives has its system
            assert frames.frame_of(frames.crs_of(frame)) == frame
    for name, system in frames.FRAMES.items():
        assert frames.crs_of(name) == system
    assert frames.crs_of("WGS84") == "EPSG:4979"  # WGS 84 is the frame of the positions nobody knows the frame of
    with pytest.raises(ValueError, match="NAD83"):
        frames.crs_of("NAD83")


def test_a_frame_and_its_epoch():
    # one check of the frame and the epoch of a position, for the photos and for the base of a PPK
    assert frames.frame_and_epoch("ITRF2020", 2026.82) == ("ITRF2020", 2026.82)
    assert frames.frame_and_epoch("ETRF2000", None) == ("ETRF2000", None)
    assert frames.frame_and_epoch("WGS84", 2026) == ("unknown", 2026.0)  # WGS 84: the frame nobody knows
    with pytest.raises(ValueError, match="epoch"):  # a position in ITRF2020 moves: it needs its epoch
        frames.frame_and_epoch("ITRF2020", None)
    with pytest.raises(ValueError, match="decimal year"):
        frames.frame_and_epoch("ETRF2000", 1.4e9)
    with pytest.raises(ValueError, match="NAD83"):
        frames.frame_and_epoch("NAD83", None)
    # the frame a record of a position names: unknown when it names none, or one the program does not know
    assert frames.position_frame({"frame": "ITRF2020", "epoch": 2026.82}) == "ITRF2020"
    assert frames.position_frame({"source": "exif"}) == frames.position_frame(None) == "unknown"
    assert frames.position_frame({"frame": "NAD83"}) == "unknown"


def test_decimal_year():
    assert frames.decimal_year(datetime(2026, 1, 1)) == 2026.0
    assert frames.decimal_year(datetime(2026, 10, 27, 12)) == pytest.approx(2026 + 299.5 / 365)  # 2026.82
    assert frames.decimal_year(datetime(2024, 7, 2)) == pytest.approx(2024 + 183 / 366)  # a leap year
    # a time that says its zone is taken in UTC: 1 a.m. on New Year's Day at UTC+2 is still the old year
    assert frames.decimal_year(datetime(2027, 1, 1, 1, tzinfo=timezone(timedelta(hours=2)))) == pytest.approx(
        2026 + (364 + 23 / 24) / 365)


# --- ITRF2020 to the European frames at the epoch ------------------------------------------------------------------


def test_itrf2020_to_rdn2008_at_the_epoch():
    p = frames.transform([[12.4964, 41.9028, 100.0]], "EPSG:9989", "EPSG:7792", epoch=2026.82)[0]
    w = crs.transform_points([[12.4964, 41.9028, 100.0]], "EPSG:4979", "EPSG:7792")[0]
    assert abs((p - w)[0] + 0.815) < 0.005 and abs((p - w)[1] + 0.634) < 0.005  # 1.03 m (the probe of 27 October)
    # crs.transform_points takes the same way for a system in ITRF2020
    assert np.allclose(crs.transform_points(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0], p, atol=1e-9)


def test_itrf2020_without_epoch_refused():
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:7792")
    with pytest.raises(ValueError, match="epoch"):
        crs.transform_points(ROME, "EPSG:9989", "EPSG:7792")
    with pytest.raises(ValueError, match="epoch"):  # the other way too
        crs.transform_points([[292335.0, 4642015.0, 100.0]], "EPSG:7792", "EPSG:9989")
    # positions of RDN2008 into a system of ITRF2020: the epoch is the one to give them, in the system they go to
    with pytest.raises(ValueError, match="EPSG:9988 is a system of ITRF2020") as refused:
        crs.transform_points([[292335.0, 4642015.0, 100.0]], "EPSG:7792", "EPSG:9988")
    assert "positions in ITRF2020" not in str(refused.value)
    # one epoch per position: none may be missing, and an epoch is a year
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME * 2, "EPSG:9989", "EPSG:7792", epoch=[2026.82, None])
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=1.4e9)  # seconds, not a year


def test_itrf2020_to_etrs89_utm_is_finite():
    p = frames.transform(ROME, "EPSG:9989", "EPSG:25833", epoch=2026.82)[0]
    w = crs.transform_points(ROME, "EPSG:4979", "EPSG:25833")[0]
    assert np.isfinite(p).all()
    shift = (frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0]
             - crs.transform_points(ROME, "EPSG:4979", "EPSG:7792")[0])
    assert np.allclose((p - w)[:2], shift[:2], atol=0.005)  # the same shift as to RDN2008


def test_itrf2020_to_wgs84_utm_unchanged():
    w = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633")
    # ITRF2020 is WGS 84 for the program: no Helmert, no epoch needed, nothing moves at any epoch
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:32633"), w, atol=1e-6)
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:32633", epoch=2026.82), w, atol=1e-6)
    assert np.allclose(crs.transform_points(w, "EPSG:32633", "EPSG:9989"), ROME, atol=1e-9)


def test_one_epoch_per_position():
    both = frames.transform(ROME * 2, "EPSG:9989", "EPSG:7792", epoch=[2015.0, 2026.82])
    assert np.allclose(both[0], frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2015.0)[0], atol=1e-9)
    assert np.allclose(both[1], frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0], atol=1e-9)
    assert np.linalg.norm(both[1, :2] - both[0, :2]) == pytest.approx(0.32, abs=0.01)  # 2.7 cm a year at Rome


def test_back_to_itrf2020_at_the_epoch():
    there = frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)
    back = frames.transform(there, "EPSG:7792", "EPSG:9989", epoch=2026.82)
    assert np.allclose(back[0, :2], ROME[0][:2], atol=1e-9) and back[0, 2] == pytest.approx(100.0, abs=1e-4)
    # a position in IGS20 is a position in ITRF2020
    assert np.allclose(frames.transform(ROME, "EPSG:10177", "EPSG:7792", epoch=2026.82), there, atol=1e-6)
    # between two systems of ITRF2020 nothing but conversions: no epoch
    xyz = frames.transform(ROME, "EPSG:9989", "EPSG:9988")
    assert np.allclose(xyz, Transformer.from_crs("EPSG:9989", "EPSG:9988", always_xy=True).transform(*ROME[0]),
                       atol=1e-6)


def _checked(src: str, dst: str, west: float, south: float, east: float, north: float):
    """The operation PROJ ranks first from ``src`` to ``dst`` among those that are no ballpark, in an area."""
    group = TransformerGroup(src, dst, always_xy=True, allow_ballpark=False,
                             area_of_interest=AreaOfInterest(west, south, east, north))
    return group.transformers[0]


def test_other_datums_through_etrf2000_or_wgs84():
    # Monte Mario (Gauss-Boaga): PROJ has a transformation from ETRF2000 in Italy, the one checked where the points
    # are (no ballpark), taken after the Helmert
    etrf = frames.transform(ROME, "EPSG:9989", "EPSG:7931", epoch=2026.82)
    gauss_boaga = np.array(_checked("EPSG:7931", "EPSG:3004", 12.49, 41.90, 12.50, 41.91).transform(*etrf[0]))
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:3004", epoch=2026.82)[0], gauss_boaga, atol=1e-6)
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:3004")
    assert np.allclose(frames.transform([gauss_boaga], "EPSG:3004", "EPSG:9989", epoch=2026.82), ROME, atol=1e-7)
    # with heights above a geoid the datum is reached the same way: the same plan position
    compound = frames.transform(ROME, "EPSG:9989", "EPSG:3004+3855", epoch=2026.82)[0]
    assert np.allclose(compound[:2], gauss_boaga[:2], atol=1e-6)
    # ED50 / UTM 33N in Campania: none from ETRF2000 there, the one from WGS 84 (ITRF2020 as it is), without an epoch
    campania = [[14.0, 41.0, 10.0]]
    ed50 = frames.transform(campania, "EPSG:9989", "EPSG:23033")
    assert np.allclose(ed50[0], _checked("EPSG:4979", "EPSG:23033", 13.99, 40.99, 14.01, 41.01).transform(*campania[0]),
                       atol=1e-6)
    assert np.allclose(frames.transform(ed50, "EPSG:23033", "EPSG:9989"), campania, atol=1e-7)


@pytest.mark.filterwarnings("ignore:Best transformation is not available")  # RDNAPTRANS, as for every position
@pytest.mark.parametrize("target, plan, place", [("EPSG:3004+3855", "EPSG:3004", [12.4964, 41.9028, 100.0]),
                                                ("EPSG:2056+5728", "EPSG:2056", [7.4474, 46.9480, 600.0]),
                                                ("EPSG:7415", "EPSG:28992", [5.3872, 52.1552, 50.0])])
def test_heights_into_a_national_datum_as_the_static_route(target, plan, place):
    """Into a system with heights above a geoid on a datum outside the frames (Monte Mario, CH1903+, Amersfoort) a
    position in ITRF2020 gets its height as every other position does (the static route): the ellipsoidal height of
    the local datum is not taken for a height above the geoid (46-50 m lower without the geoid grid). The plan position
    is the one of the horizontal system. With the grid see test_heights_with_a_geoid_grid_as_the_static_route."""
    static = crs.transform_points([place], "EPSG:4979", target)[0]
    framed = frames.transform([place], "EPSG:9989", target, epoch=2026.82)[0]
    # the height of the static path for the point of ETRF2000 (the Helmert moves it by centimetres) read as WGS 84
    etrf2000 = frames.transform([place], "EPSG:9989", "EPSG:7931", epoch=2026.82)[0]
    assert framed[2] == pytest.approx(crs.transform_points([etrf2000], "EPSG:4979", target)[0][2], abs=1e-6)
    assert framed[2] == pytest.approx(static[2], abs=0.05)  # the static route's own: no 46-50 m drop
    assert np.allclose(framed[:2], frames.transform([place], "EPSG:9989", plan, epoch=2026.82)[0][:2], atol=1e-6)
    assert np.allclose(frames.transform([framed], target, "EPSG:9989", epoch=2026.82)[0], place, atol=1e-7)


# a synthetic EGM2008 (45 m everywhere over Europe) under the name PROJ looks for, in a folder PROJ is told about, then
# heights above it from ITRF2020 and from WGS 84, in a national datum and in RDN2008: a process of its own, as PROJ
# keeps the grids it found
_WITH_A_GRID = """
import json, sys
from pathlib import Path
import pyarchinit_photogrammetry  # its PROJ data first, as the program
import numpy as np, pyproj, rasterio
from rasterio.transform import from_origin
from pyarchinit_photogrammetry.core import crs
from pyarchinit_photogrammetry.gnss import frames
folder = Path(sys.argv[1])
with rasterio.open(folder / "us_nga_egm08_25.tif", "w", driver="GTiff", width=21, height=21, count=1,
                   dtype="float32", crs="EPSG:4326", transform=from_origin(-0.5, 55.5, 1.0, 1.0)) as grid:
    grid.write(np.full((1, 21, 21), 45.0, dtype="float32"))
    grid.update_tags(TYPE="VERTICAL_OFFSET_GEOGRAPHIC_TO_VERTICAL")
    grid.set_band_description(1, "geoid_undulation")
    grid.update_tags(1, UNITTYPE="metre")
pyproj.datadir.append_data_dir(str(folder))
place, out = [12.4964, 41.9028, 100.0], {}
for target in ("EPSG:3004+3855", "EPSG:7792+3855"):
    static = crs.transform_points([place], "EPSG:4979", target)[0]
    framed = frames.transform([place], "EPSG:9989", target, epoch=2026.82)[0]
    back = frames.transform([framed], target, "EPSG:9989", epoch=2026.82)[0]
    out[target] = {"static": static.tolist(), "framed": framed.tolist(), "back": back.tolist()}
print(json.dumps(out))
"""


def test_heights_with_a_geoid_grid_as_the_static_route(tmp_path):
    """With the geoid grid of the target there, a position in ITRF2020 gets the height above the geoid the static route
    gives - in a national datum (Monte Mario + EGM2008) as in RDN2008 - not its ellipsoidal height (48 m off at Rome
    with the real EGM2008); and back to ITRF2020 the ellipsoidal height it had."""
    run = subprocess.run([sys.executable, "-c", _WITH_A_GRID, str(tmp_path)], capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    for target, heights in out.items():
        assert heights["static"][2] == pytest.approx(55.0, abs=1e-6), target  # the grid was found: 100 - 45
        assert heights["framed"][2] == pytest.approx(heights["static"][2], abs=0.01), target
        assert np.allclose(heights["back"][:2], ROME[0][:2], atol=1e-7), target
        assert heights["back"][2] == pytest.approx(100.0, abs=1e-3), target


def test_rows_without_a_position_pass_through():
    # a row with no number is no position, as PROJ has it: it stays without one, needs no epoch, stops nothing
    rows = [[np.nan, np.nan, np.nan], ROME[0], [12.5, np.nan, 100.0]]
    one = frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0]
    for epoch in (2026.82, [None, 2026.82, None]):
        out = frames.transform(rows, "EPSG:9989", "EPSG:7792", epoch=epoch)
        assert np.isnan(out[0]).all() and np.isnan(out[2]).all() and np.allclose(out[1], one, atol=1e-9)
    assert np.isnan(frames.transform(rows[:1], "EPSG:9989", "EPSG:7792")).all()  # nothing to move, no epoch asked


def test_unknown_datum_refused():
    unknown = "+proj=longlat +ellps=intl +no_defs +type=crs"  # a datum PROJ knows only by a ballpark
    with pytest.raises(ValueError, match="ballpark"):
        frames.transform(ROME, "EPSG:9989", unknown, epoch=2026.82)
    with pytest.raises(ValueError, match="ballpark") as refused:  # the other way: said of the system it goes to
        crs.transform_points(ROME, unknown, "EPSG:9989", epoch=2026.82)
    assert "EPSG:9989, a system of ITRF2020" in str(refused.value)
    assert "positions in ITRF2020 from" not in str(refused.value)


def test_a_ballpark_is_never_taken_at_the_edge_of_a_transformation(monkeypatch):
    """PROJ's own transformation from ITRF2020 is used only for the points inside the area of use of one that is no
    ballpark: at the edge of the European ones (east of 38.01 E, south of 33.26 N) PROJ would take its ballpark, so
    those points go as positions on WGS 84."""
    given = []
    by_proj = crs.by_proj

    def spy(points, src, dst):
        given.append((np.atleast_2d(np.asarray(points, float)).copy(), src))
        return by_proj(points, src, dst)

    monkeypatch.setattr(crs, "by_proj", spy)
    points = np.array([[37.9, 50.0, 100.0], [39.5, 50.0, 100.0], [10.0, 33.27, 10.0], [10.0, 33.256, 10.0]])
    frames.legacy_transform(points, "EPSG:9989", "EPSG:3035", frames.Legacy("test points"))
    own = np.vstack([rows for rows, src in given if src == "EPSG:9989"])
    assert np.array_equal(own, points[[0, 2]])  # inside the area: by PROJ's own route, as before
    proj = Transformer.from_crs("EPSG:9989", "EPSG:3035", always_xy=True)
    for row in own:  # and there PROJ's own route is no ballpark
        proj.transform(*row)
        assert "Ballpark" not in proj.get_last_used_operation().description


# --- the positions of the photos -----------------------------------------------------------------------------------


def _camera(key: int, when: str = "2026:10:27 12:00:00") -> Camera:
    return Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0, meta={"datetime": when})


def test_every_position_with_its_frame_and_epoch():
    cam = _camera(0)
    gnss_apply.set_camera_position(cam, [14.0, 41.0, 100.0], "EPSG:4326", [5.0, 5.0, 10.0], "exif")
    assert (cam.meta["gnss"]["frame"], cam.meta["gnss"]["epoch"]) == ("unknown", None)  # WGS 84: nobody knows
    # a positions file keeps its own system, the frame of its datum
    gnss_apply.set_camera_position(cam, [292335.0, 4642015.0, 100.0], "EPSG:7792", [0.02] * 3, "positions_file")
    assert cam.meta["reference_crs"] == "EPSG:7792" and cam.meta["gnss"]["frame"] == "ETRF2000"
    gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], frames.crs_of("ITRF2020"), [0.02] * 3, "ppk",
                                   frame="ITRF2020", epoch=2026.82)
    assert cam.meta["reference_crs"] == "EPSG:9989"
    assert (cam.meta["gnss"]["frame"], cam.meta["gnss"]["epoch"]) == ("ITRF2020", 2026.82)
    before = (cam.reference, json.dumps(cam.meta))
    with pytest.raises(ValueError, match="epoch"):  # an ITRF2020 position without its epoch fails where it is written
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:9989", [0.02] * 3, "ppk")
    with pytest.raises(ValueError, match="ETRF2000"):  # a frame that is not the one of the system
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:4979", [0.02] * 3, "ppk", frame="ETRF2000",
                                       epoch=2026.82)
    with pytest.raises(ValueError, match="NAD83"):
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:4979", [0.02] * 3, "ppk", frame="NAD83")
    # the system and the frame disagree: said first, before the epoch that is missing too
    with pytest.raises(ValueError, match="the frame of a position in EPSG:4979 is unknown, not ITRF2020"):
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:4979", [0.02] * 3, "ppk", frame="ITRF2020")
    assert (cam.reference, json.dumps(cam.meta)) == before  # nothing changed


def test_camera_references_use_each_epoch():
    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:7792"
    epochs = (2015.0, 2026.82)
    for key, epoch in enumerate(epochs):  # the same place measured 11.8 years apart
        cam = _camera(key)
        gnss_apply.set_camera_position(cam, list(ROME[0]), "EPSG:9989", [0.02, 0.02, 0.04], "ppk", status="fix",
                                       height="ellipsoidal", frame="ITRF2020", epoch=epoch)
        ch.cameras.append(cam)
    positions, system = gcp.camera_references(ch, ch.cameras, ch.crs)
    errors = gcp.camera_reference_errors(ch)
    assert system == "EPSG:7792"
    for cam, epoch in zip(ch.cameras, epochs):
        expected = frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=epoch)[0]
        assert np.allclose(positions[cam.key], expected, atol=1e-6)
        assert np.allclose(errors[cam.key]["reference"], expected, atol=1e-6)
    assert np.linalg.norm(positions[1][:2] - positions[0][:2]) == pytest.approx(0.32, abs=0.01)


@pytest.mark.filterwarnings("ignore:Best transformation is not available")  # PROJ's own route to ETRS89, as before
@pytest.mark.parametrize("system", ["EPSG:7792", "EPSG:25833"])
def test_old_projects_as_before(system, caplog):
    """Positions written before the frames - a record without frame and epoch, or no record at all - convert as they
    always did: PROJ's own choice from their system to the one of the chunk, bit for bit, also those of a positions
    file typed in ITRF2020 with the 1.6.0 (into RDN2008 the WGS 84 route gives the very numbers of PROJ's ballpark,
    into ETRS89 PROJ has a transformation of its own), with a warning said once for the chunk."""
    ch = ppg.Document().add_chunk()
    ch.crs = system
    places = [("EPSG:4326", [12.4964, 41.9028, 100.0]), ("EPSG:4979", [12.4970, 41.9030, 101.0]),
              ("EPSG:32633", [292400.0, 4642100.0, 102.0]), ("EPSG:3004", [2312339.2, 4642025.8, 103.0]),
              ("EPSG:9989", [12.4980, 41.9040, 104.0]), ("EPSG:9989", [12.4990, 41.9010, 105.0])]
    for key, (source, place) in enumerate(places):
        cam = _camera(key)
        cam.reference = Reference(location=list(place), accuracy=[1.0, 1.0, 2.0], enabled=True)
        cam.meta["reference_crs"] = source
        if key not in (3, 5):  # the record of a position written by the 1.6.0; two have none at all
            cam.meta["gnss"] = {"source": "positions_file", "status": None, "accuracy": [1.0, 1.0, 2.0],
                                "height": "ellipsoidal"}
        ch.cameras.append(cam)
    before = [np.array(Transformer.from_crs(source, system, always_xy=True).transform(*place))
              for source, place in places]
    with caplog.at_level(logging.WARNING, logger=FRAMES_LOG):
        positions, _system = gcp.camera_references(ch, ch.cameras, ch.crs)
        errors = gcp.camera_reference_errors(ch)
        (flight,) = quality.summary(ch)
    for cam, expected in zip(ch.cameras, before):
        assert np.array_equal(positions[cam.key], expected) and np.array_equal(errors[cam.key]["reference"], expected)
    assert flight["frames"] == {"unknown": 6}
    # said once for the chunk, not once a photo nor once a call: how, how far off, and what to do for the photos
    (said,) = [r.getMessage() for r in caplog.records if r.name == FRAMES_LOG]
    assert "EPSG:9989" in said and "about a metre" in said and "WGS 84" in said and "approximation" in said
    assert "again with their epoch" in said and "ETRF2000" in said
    # the georeference fitted on them puts the cameras where PROJ puts their references
    offset = np.mean(before, axis=0)
    for cam, expected in zip(ch.cameras, before):
        cam.transform = np.eye(4)
        cam.transform[:3, 3] = expected - offset
    assert gcp.update_transform(ch)["method"] == "cameras"
    for cam, expected in zip(ch.cameras, before):
        assert np.allclose(ch.to_world(cam.center), expected, atol=1e-6)


def test_markers_in_itrf2020_convert_as_before(tmp_path, caplog):
    """Markers carry no frame and no epoch: control points in ITRF2020 go into a chunk on RDN2008, and from the system
    of one chunk to another's, as before (bit for bit), with the warning, said for each chunk."""
    points = [[12.4964, 41.9028, 100.0], [12.4970, 41.9030, 101.0], [12.4980, 41.9020, 102.0]]
    table = tmp_path / "gcp.csv"
    table.write_text("".join(f"P{i},{x},{y},{z}\n" for i, (x, y, z) in enumerate(points)))
    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:7792"
    to_rdn = Transformer.from_crs("EPSG:9989", "EPSG:7792", always_xy=True)
    with caplog.at_level(logging.WARNING, logger=FRAMES_LOG):
        assert gcp.import_reference(ch, table, crs="EPSG:9989") == 3
        for m, point in zip(ch.markers, points):
            assert np.array_equal(m.reference.location, to_rdn.transform(*point))
        chunks._convert_references(ch, "EPSG:7792", "EPSG:9989")
        other = ppg.Document().add_chunk()  # another project: its own warning, not one for the whole session
        other.crs = "EPSG:7792"
        assert gcp.import_reference(other, table, crs="EPSG:9989") == 3
    back = Transformer.from_crs("EPSG:7792", "EPSG:9989", always_xy=True)
    for m, point in zip(ch.markers, points):
        assert np.array_equal(m.reference.location, back.transform(*to_rdn.transform(*point)))
    said = [r.getMessage() for r in caplog.records if r.name == FRAMES_LOG]
    assert len(said) == 3  # the import and the conversion of the chunks in the first, the import in the other
    # markers have no epoch to give them: the advice is a frame
    assert all("coordinate system of the chunk" in text and "ETRF2000" in text and "again with their epoch" not in text
               for text in said)


@pytest.mark.parametrize("system, place", [("EPSG:3004", [12.4964, 41.9028, 100.0]),
                                           ("EPSG:2056", [7.4474, 46.9480, 600.0])])
def test_old_positions_never_take_a_ballpark(system, place, tmp_path):
    """Where PROJ's own route from ITRF2020 is a ballpark (Monte Mario, CH1903+: 73 m off at Rome, 163 m at Bern with
    the 1.6.0), positions and markers that carry no frame go as positions on WGS 84, as every position whose frame
    nobody knows: within the accuracy of the transformation of the datum, metres, not a hundred."""
    ch = ppg.Document().add_chunk()
    ch.crs = system
    cam = _camera(0)
    cam.reference = Reference(location=list(place), accuracy=[1.0, 1.0, 2.0], enabled=True)
    cam.meta["reference_crs"] = "EPSG:9989"
    cam.meta["gnss"] = {"source": "positions_file", "status": None, "accuracy": [1.0] * 3, "height": "ellipsoidal"}
    ch.cameras.append(cam)
    as_wgs84 = crs.transform_points([place], "EPSG:4979", system)[0]
    ballpark = np.array(Transformer.from_crs("EPSG:9989", system, always_xy=True).transform(*place))
    assert np.linalg.norm(ballpark[:2] - as_wgs84[:2]) > 70.0  # what the 1.6.0 gave
    positions, _system = gcp.camera_references(ch, ch.cameras, ch.crs)
    assert np.allclose(positions[cam.key], as_wgs84, atol=1e-6)
    table = tmp_path / "gcp.csv"
    table.write_text("P,{},{},{}\n".format(*place))
    assert gcp.import_reference(ch, table, crs="EPSG:9989") == 1
    assert np.allclose(ch.markers[0].reference.location, as_wgs84, atol=1e-6)


def test_the_frames_of_a_flight():
    ch = ppg.Document().add_chunk()
    ch.cameras = [_camera(k, f"2026:10:27 12:0{k}:00") for k in range(4)]
    gnss_apply.set_camera_position(ch.cameras[0], list(ROME[0]), "EPSG:9989", [0.02] * 3, "ppk", frame="ITRF2020",
                                   epoch=2026.82)
    gnss_apply.set_camera_position(ch.cameras[1], [292335.0, 4642015.0, 100.0], "EPSG:7792", [0.02] * 3,
                                   "positions_file")
    gnss_apply.set_camera_position(ch.cameras[2], [12.5, 41.9, 100.0], "EPSG:4326", [5.0, 5.0, 10.0], "exif")
    (flight,) = quality.summary(ch)  # the photo 3 has no position: in none of them
    assert flight["frames"] == {"ITRF2020": 1, "ETRF2000": 1, "unknown": 1}


# --- the base of a PPK solution and the photos it places -----------------------------------------------------------


def test_solve_with_the_position_of_a_station(tmp_path, request):
    runs = request.getfixturevalue("fake_rtklib")
    station = ppk.BasePosition(35.16, 139.61, 70.0, "station", frame="ITRF2020", epoch=2005.25,
                               sd=(0.002, 0.002, 0.005), detail={"station": "0759"})
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], station, folder=tmp_path / "work")
    options = _options(runs()[-1]["options"])
    assert [float(options[f"ant2-pos{i}"]) for i in (1, 2, 3)] == [35.16, 139.61, 70.0]
    base = solution.base
    assert base["position"] == [139.61, 35.16, 70.0] and base["position_source"] == "station"
    assert (base["frame"], base["epoch"], base["sd"], base["detail"]) == ("ITRF2020", 2005.25, [0.002, 0.002, 0.005],
                                                                          {"station": "0759"})
    # three numbers are typed by the user, in a frame nobody knows
    typed = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=tmp_path / "work").base
    assert typed["position_source"] == "typed" and typed["position"] == [139.61, 35.16, 70.0]
    assert (typed["frame"], typed["epoch"], typed["sd"], typed["detail"]) == ("unknown", None, None, {})
    header = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=tmp_path / "work").base
    assert header["position_source"] == "header" and header["frame"] == "unknown" and header["epoch"] is None
    # a base in ITRF2020 without its epoch, a source or a frame the program does not know, a latitude off the earth
    with pytest.raises(ValueError, match="epoch"):
        ppk.BasePosition(35.16, 139.61, 70.0, "station", frame="ITRF2020")
    with pytest.raises(ValueError, match="guess"):
        ppk.BasePosition(35.16, 139.61, 70.0, "guess")
    with pytest.raises(ValueError, match="NAD83"):
        ppk.BasePosition(35.16, 139.61, 70.0, "typed", frame="NAD83")
    with pytest.raises(ValueError, match="latitude"):
        ppk.BasePosition(135.16, 139.61, 70.0, "typed")
    # the detail goes into every photo, saved with the project: only what JSON writes
    with pytest.raises(ValueError, match="JSON"):
        ppk.BasePosition(35.16, 139.61, 70.0, "station", detail={"when": datetime(2026, 10, 27)})
    with pytest.raises(ValueError, match="JSON"):
        ppk.BasePosition(35.16, 139.61, 70.0, "station", detail=["0759"])


def test_apply_ppk_writes_frame_and_epoch(tmp_path):
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    shots = [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (2, 3, 4)]

    # a solution read from a file knows no base: its positions in a frame nobody knows, at the epochs of the shots
    folder = _ppk_flight(tmp_path / "file", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["matched"] == 3
    for cam in ch.cameras:
        g = cam.meta["gnss"]
        assert cam.meta["reference_crs"] == "EPSG:4979" and g["frame"] == "unknown"
        assert g["epoch"] == pytest.approx(frames.decimal_year(datetime.fromisoformat(g["utc"])), abs=1e-12)

    # a base in ITRF2020 (a permanent station): the photos in ITRF2020 at the epoch of the coordinates of the base
    solution.base = {"name": "STAZ", "position": [139.62, 35.13, 60.0], "position_source": "station",
                     "frame": "ITRF2020", "epoch": 2005.25, "sd": [0.002, 0.002, 0.005], "detail": {"station": "STAZ"}}
    folder = _ppk_flight(tmp_path / "station", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["matched"] == 3
    for cam in ch.cameras:
        g = cam.meta["gnss"]
        assert cam.meta["reference_crs"] == "EPSG:9989" and g["frame"] == "ITRF2020" and g["base"] == solution.base
        assert g["epoch"] == 2005.25
    json.dumps(ch.meta)
    for cam in ch.cameras:
        json.dumps(cam.meta)  # saved with the project

    # a frame the program does not know, a base in ITRF2020 without the epoch of its coordinates: refused, nothing
    # changed
    before = _snapshot(ch)
    for wrong, said in (({"frame": "NAD83"}, "NAD83"), ({"epoch": None}, "epoch")):
        solution.base = {**solution.base, "frame": "ITRF2020", "epoch": 2005.25, **wrong}
        with pytest.raises(ValueError, match=said):
            gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
        assert _snapshot(ch) == before


def _rome_flight(folder: Path, start: float, count: int) -> Path:
    """A DJI flight over Rome: ``count`` photos DJI_0001.JPG ... a second apart from ``start`` (seconds of GPS time,
    in 2026: 18 s ahead of UTC), with the time their camera wrote (UTC+1), and their Timestamp.MRK."""
    folder.mkdir(parents=True)
    lines = []
    for index in range(1, count + 1):
        gps = start + index
        week, seconds = divmod(gps, 604800.0)
        _jpeg(folder / f"DJI_{index:04d}.JPG", None, when=datetime(1980, 1, 6) + timedelta(seconds=gps - 18, hours=1))
        lines.append(f"{index}\t{seconds:.6f}\t[{int(week)}]\t     0,N\t     0,E\t   150,V\t41.90280000,Lat\t"
                     "12.49640000,Lon\t100.000,Ellh\t0.010, 0.010, 0.020\t50,Q\n")
    (folder / "Timestamp.MRK").write_text("".join(lines), newline="\n")
    return folder


def test_ppk_photos_carry_the_epoch_of_the_base_coordinates(tmp_path):
    """A position of the rover is the base's coordinates plus the baseline: with a base in ITRF2020 at 2015.0 (the
    coordinates of a station, not carried to the day of the flight) the photos of a flight of 2026 are in ITRF2020 at
    2015.0, and go to RDN2008 with that epoch - 32 cm from where the epoch of the flight would put them."""
    start = (datetime(2026, 10, 27, 10) - datetime(1980, 1, 6)).total_seconds()  # GPS time
    folder = _rome_flight(tmp_path / "100MEDIA", start, 3)
    solution = ppk.Solution(times=start + np.arange(5.0), llh=np.tile([41.9028, 12.4964, 100.0], (5, 1)),
                            q=np.ones(5, dtype=int), ns=np.full(5, 9), sd=np.full((5, 3), 0.003),
                            base={"name": "M0SE", "position": [12.4935, 41.8935, 100.0], "position_source": "station",
                                  "frame": "ITRF2020", "epoch": 2015.0, "sd": [0.001] * 3, "detail": {}},
                            path=Path("station.pos"))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["matched"] == 3
    assert {cam.meta["gnss"]["epoch"] for cam in ch.cameras} == {2015.0}
    ch.crs = "EPSG:7792"
    positions, _system = gcp.camera_references(ch, ch.cameras, ch.crs)
    flight = frames.decimal_year(datetime(2026, 10, 27, 10))
    for cam in ch.cameras:
        at_2015 = frames.transform([cam.reference.location], "EPSG:9989", "EPSG:7792", epoch=2015.0)[0]
        at_flight = frames.transform([cam.reference.location], "EPSG:9989", "EPSG:7792", epoch=flight)[0]
        assert np.allclose(positions[cam.key], at_2015, atol=1e-9)
        assert np.linalg.norm(at_flight[:2] - at_2015[:2]) == pytest.approx(0.32, abs=0.01)
