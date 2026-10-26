"""Precise camera positions: the RTK status of DJI photos, their Timestamp.MRK and the accuracy they give."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image
from pyproj import Transformer

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import dji


def _xmp(body: str) -> str:
    return ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" {body}'
            '</rdf:Description></rdf:RDF></x:xmpmeta>')


def test_rtk_status_from_dji_xmp():
    text = _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.01234" drone-dji:RtkStdLat="0.01100" '
                'drone-dji:RtkStdHgt="0.02300" drone-dji:RtkDiffAge="1.5">')
    rtk = dji.rtk_from_xmp(text)
    assert rtk["flag"] == 50 and rtk["status"] == "fix"
    assert rtk["std"] == pytest.approx([0.01234, 0.011, 0.023])
    assert rtk["diff_age"] == pytest.approx(1.5)

    elements = _xmp("><drone-dji:RtkFlag>34</drone-dji:RtkFlag><drone-dji:RtkStdLon>0.05</drone-dji:RtkStdLon>"
                    "<drone-dji:RtkStdLat>0.06</drone-dji:RtkStdLat><drone-dji:RtkStdHgt>0.09</drone-dji:RtkStdHgt>")
    rtk = dji.rtk_from_xmp(elements)
    assert rtk["status"] == "float" and rtk["std"] == pytest.approx([0.05, 0.06, 0.09])
    assert rtk["diff_age"] is None

    assert dji.rtk_from_xmp(_xmp('drone-dji:GimbalYawDegree="12.3">')) is None
    assert dji.rtk_from_xmp("") is None


def test_rtk_flags_and_missing_standard_deviations():
    assert dji.STATUS == {50: "fix", 34: "float", 16: "single", 0: "none"}
    assert dji.rtk_from_xmp(_xmp('drone-dji:RtkFlag="16">'))["std"] is None
    assert dji.rtk_from_xmp(_xmp('drone-dji:RtkFlag="0">'))["status"] == "none"
    assert dji.rtk_from_xmp(_xmp('drone-dji:RtkFlag="7">'))["status"] == "unknown"
    assert dji.rtk_from_xmp(_xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="n/a">'))["std"] is None


def test_accuracy_from_status():
    assert gnss_apply.accuracy("fix", [0.004, 0.004, 0.009]) == pytest.approx([0.01, 0.01, 0.018])
    assert gnss_apply.accuracy("float", [0.05] * 3) == pytest.approx([0.2] * 3)
    assert gnss_apply.accuracy("single", [1.0, 1.3, 2.3]) == pytest.approx([2.0, 2.6, 4.6])
    assert gnss_apply.accuracy("none", [0.004] * 3) == [5.0, 5.0, 10.0]
    assert gnss_apply.accuracy(None, None) == [5.0, 5.0, 10.0]
    assert gnss_apply.accuracy("fix", None) == pytest.approx([0.01] * 3)
    assert gnss_apply.accuracy("unknown", [0.1, 0.1, 0.1]) == pytest.approx([1.0] * 3)


def test_set_camera_position_records_where_it_comes_from():
    cam = ppg.core.camera.Camera(key=0, label="a", path="a.jpg", sensor_key=0)
    gnss_apply.set_camera_position(cam, [14.0, 41.0, 100.0], "EPSG:4326", [0.01, 0.01, 0.02], "dji_xmp",
                                   status="fix", height="ellipsoidal", extra={"diff_age": 1.0})
    assert cam.reference.location == [14.0, 41.0, 100.0] and cam.reference.enabled
    assert cam.reference.accuracy == [0.01, 0.01, 0.02]
    assert cam.meta["reference_crs"] == "EPSG:4326"
    assert cam.meta["gnss"] == {"source": "dji_xmp", "status": "fix", "accuracy": [0.01, 0.01, 0.02],
                                "height": "ellipsoidal", "diff_age": 1.0}


def _jpeg(path, xmp: str | None):
    img = Image.fromarray(np.random.default_rng(0).integers(0, 255, (48, 64, 3), dtype=np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (41, 8, 0.0), 3: "E", 4: (14, 15, 0.0), 5: 0, 6: 123.4})
    kwargs = {"exif": exif}
    if xmp is not None:
        kwargs["xmp"] = xmp.encode()
    img.save(path, **kwargs)
    return path


def test_added_photos_take_their_rtk_accuracy(tmp_path):
    rtk = _jpeg(tmp_path / "rtk.jpg", _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.004" '
                                          'drone-dji:RtkStdLat="0.004" drone-dji:RtkStdHgt="0.009">'))
    plain = _jpeg(tmp_path / "plain.jpg", None)
    ch = ppg.Document().add_chunk()
    cams = {c.label: c for c in ch.add_photos([rtk, plain])}

    cam = cams["rtk"]
    assert cam.reference.accuracy == pytest.approx([0.01, 0.01, 0.018])
    assert cam.reference.enabled and cam.meta["reference_crs"] == "EPSG:4326"
    assert cam.reference.location[2] == pytest.approx(123.4)
    assert cam.meta["gnss"]["status"] == "fix" and cam.meta["gnss"]["source"] == "dji_xmp"
    assert cam.meta["gnss"]["height"] == "exif"

    cam = cams["plain"]
    assert cam.reference.accuracy == [5.0, 5.0, 10.0]
    assert cam.meta["gnss"]["source"] == "exif" and cam.meta["gnss"]["status"] is None
    assert cam.meta["gnss"]["height"] == "exif"
    assert cam.meta["reference_crs"] == "EPSG:4326"


# --- Timestamp.MRK -------------------------------------------------------------------------------------------

MRK_LINES = (
    "1\t357163.425624\t[2087]\t    -2,N\t    11,E\t   189,V\t22.53974306,Lat\t113.94255764,Lon\t17.522,Ellh\t"
    "0.012012, 0.010939, 0.022478\t50,Q\n"
    "2\t406432.500000\t[2106]\t   120,N\t  -330,E\t   -15,V\t41.13400012,Lat\t14.26600034,Lon\t94.350,Ellh\t"
    "0.9, 1.1, 2.0\t16,Q\n"
    "3\t406435.000000\t[2106]\t     0,N\t     0,E\t   150,V\t-33.50000000,Lat\t-70.60000000,Lon\t-1.250,Ellh\t"
    "0.05, 0.06, 0.09\t34,Q\n")


def _shot(offset_ned_mm=(0.0, 0.0, 0.0), lat=41.134, lon=14.266, ellh=94.35, std=(0.01, 0.01, 0.02), flag=50):
    return dji.MrkShot(index=1, gps_week=2106, gps_seconds=406432.5, offset_ned_mm=offset_ned_mm, lat=lat,
                       lon=lon, ellh=ellh, std=std, flag=flag)


def test_mrk_lines_are_read(tmp_path):
    path = tmp_path / "Timestamp.MRK"
    path.write_text(MRK_LINES + "\n")
    shots = dji.read_mrk(path)
    assert [s.index for s in shots] == [1, 2, 3]

    first = shots[0]
    assert (first.gps_week, first.gps_seconds) == (2087, pytest.approx(357163.425624))
    assert first.offset_ned_mm == pytest.approx((-2, 11, 189))
    assert (first.lat, first.lon, first.ellh) == pytest.approx((22.53974306, 113.94255764, 17.522))
    assert first.std == pytest.approx((0.012012, 0.010939, 0.022478)) and first.flag == 50

    second = shots[1]
    assert second.offset_ned_mm == pytest.approx((120, -330, -15))
    assert (second.lat, second.lon, second.ellh) == pytest.approx((41.13400012, 14.26600034, 94.35))
    assert second.std == pytest.approx((0.9, 1.1, 2.0)) and second.flag == 16
    # week 2106 + 406432.5 s of GPS time, 18 leap seconds before UTC
    assert second.utc == datetime(2020, 5, 21, 16, 53, 34, 500000, tzinfo=timezone.utc)
    assert first.utc == datetime(2020, 1, 9, 3, 12, 25, 425624, tzinfo=timezone.utc)

    third = shots[2]
    assert (third.lat, third.lon, third.ellh, third.flag) == pytest.approx((-33.5, -70.6, -1.25, 34))


def test_mrk_line_in_another_format_is_refused(tmp_path):
    path = tmp_path / "Timestamp.MRK"
    path.write_text(MRK_LINES.splitlines()[0] + "\n" + "2\t406432.5\t[2106]\t120,N\t-330,E\n")
    with pytest.raises(ValueError, match="line 2"):
        dji.read_mrk(path)


def _enu(origin, point):
    """East, north, up (m) of ``point`` from ``origin``, both (lon, lat, h) on the WGS84 ellipsoid."""
    to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    o, p = np.array(to_ecef.transform(*origin)), np.array(to_ecef.transform(*point))
    lon, lat = np.radians(origin[0]), np.radians(origin[1])
    east = np.array([-np.sin(lon), np.cos(lon), 0.0])
    north = np.array([-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)])
    up = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
    return np.array([east @ (p - o), north @ (p - o), up @ (p - o)])


def test_camera_below_the_antenna():
    shot = _shot((0, 0, 150))
    lon, lat, h = dji.camera_position(shot)
    assert lon == pytest.approx(shot.lon, abs=1e-9) and lat == pytest.approx(shot.lat, abs=1e-9)
    assert h == pytest.approx(shot.ellh - 0.150, abs=1e-9)

    lon, lat, h = dji.camera_position(_shot((100, 0, 0)))
    assert _enu((14.266, 41.134, 94.35), (lon, lat, h)) == pytest.approx([0.0, 0.1, 0.0], abs=1e-3)

    # all three at once, in the northern and the southern hemisphere, with the arm of a real file
    for lat0, lon0 in ((41.134, 14.266), (-33.5, -70.6), (22.5397, 113.9426)):
        shot = _shot((120, -330, 189), lat=lat0, lon=lon0, ellh=50.0)
        enu = _enu((lon0, lat0, 50.0), dji.camera_position(shot))
        assert enu == pytest.approx([-0.330, 0.120, -0.189], abs=1e-3)


def _numbered(line: str, index: int) -> str:
    """The MRK ``line`` with another shot number."""
    return f"{index}{line[line.index(chr(9)):]}"


def _flight(folder, names, lines=MRK_LINES, xmp_first=False):
    folder.mkdir(exist_ok=True)
    for i, name in enumerate(names):
        _jpeg(folder / name, _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.004" drone-dji:RtkStdLat="0.004" '
                                  'drone-dji:RtkStdHgt="0.009">') if xmp_first and i == 0 else None)
    (folder / "Timestamp.MRK").write_text(lines)
    return folder


def _snapshot(chunk):
    return {c.label: (c.reference.location, c.reference.accuracy, c.reference.enabled, dict(c.meta["gnss"]),
                      c.meta["reference_crs"]) for c in chunk.cameras}


def test_mrk_matched_by_shot_number_and_by_order(tmp_path):
    names = [f"DJI_20240512103015_{i:04d}_V.JPG" for i in (1, 2, 3)]
    folder = _flight(tmp_path / "100MEDIA", names, xmp_first=True)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    (folder / "DJI_20240512103015_0001_V.srt").write_text("not a photo")
    (folder / "._DJI_20240512103015_0002_V.JPG").write_bytes(b"the resource fork macOS leaves on a card")
    assert ch.camera("DJI_20240512103015_0001_V").meta["gnss"]["source"] == "dji_xmp"

    result = gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")
    assert result == {"matched": 3, "shots": 3, "photos": 3, "status": {"fix": 1, "single": 1, "float": 1},
                      "unmatched": []}

    shots = dji.read_mrk(folder / "Timestamp.MRK")
    for cam, shot in zip(ch.cameras, shots):
        assert cam.meta["reference_crs"] == "EPSG:4979" and cam.reference.enabled
        assert cam.reference.location == pytest.approx(list(dji.camera_position(shot)), abs=1e-12)
        g = cam.meta["gnss"]
        assert (g["source"], g["height"], g["mrk"], g["shot"]) == ("dji_mrk", "ellipsoidal", "Timestamp.MRK",
                                                                   shot.index)
        assert g["utc"] == shot.utc.isoformat() and g["status"] == dji.STATUS[shot.flag]
    # twice the standard deviation of east, north, height (the order of the position: lon, lat, height)
    fix, single, flt = ch.cameras
    assert fix.reference.accuracy == pytest.approx([0.021878, 0.024024, 0.044956])
    assert single.reference.accuracy == pytest.approx([2.2, 1.8, 4.0])
    assert flt.reference.accuracy == pytest.approx([0.2, 0.2, 0.2])
    assert fix.meta["gnss"]["accuracy"] == fix.reference.accuracy

    again = _snapshot(ch)
    assert gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK") == result
    assert _snapshot(ch) == again


def test_mrk_missing_a_shot_never_moves_the_others(tmp_path):
    names = [f"DJI_{i:04d}.JPG" for i in (1, 2, 3, 4)]
    first, second, third = MRK_LINES.splitlines(keepends=True)
    # the MRK lacks shot 1 and has a shot 5 no photo belongs to: by order everything would be shifted by one
    lines = [second, third, _numbered(first, 4), _numbered(first, 5)]
    folder = _flight(tmp_path / "100MEDIA", names, lines="".join(lines))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    result = gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")
    assert (result["matched"], result["shots"], result["photos"], result["unmatched"]) == (3, 4, 4, ["DJI_0001"])
    assert ch.camera("DJI_0002").meta["gnss"]["shot"] == 2
    assert ch.camera("DJI_0003").meta["gnss"]["shot"] == 3
    assert ch.camera("DJI_0004").meta["gnss"]["shot"] == 4
    assert ch.camera("DJI_0001").meta["gnss"]["source"] == "exif"


def test_mrk_by_order_only_with_the_same_number_of_shots(tmp_path):
    folder = _flight(tmp_path / "photos", ["a.JPG", "b.JPG", "c.JPG"])
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    result = gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")
    assert (result["matched"], result["unmatched"]) == (3, [])
    assert [c.meta["gnss"]["shot"] for c in ch.cameras] == [1, 2, 3]

    (folder / "Timestamp.MRK").write_text(MRK_LINES + _numbered(MRK_LINES.splitlines(keepends=True)[0], 4))
    with pytest.raises(ValueError, match="4 shots.*3 photos"):
        gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")

    # the MRK can be elsewhere: the photos folder says where the photos are
    elsewhere = tmp_path / "card"
    elsewhere.mkdir()
    (elsewhere / "Timestamp.MRK").write_text(MRK_LINES)
    assert gnss_apply.apply_mrk(ch, elsewhere / "Timestamp.MRK", str(folder))["matched"] == 3
