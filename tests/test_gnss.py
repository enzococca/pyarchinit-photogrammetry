"""Precise camera positions: the RTK status of DJI photos, their Timestamp.MRK and the accuracy they give."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

import numpy as np
import pytest
from PIL import Image
from pyproj import Transformer

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import dji, positions, quality


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
    # a standard deviation that is not a finite number is none: the accuracy is the floor of the status
    for bad in ("nan", "inf", "-inf"):
        rtk = dji.rtk_from_xmp(_xmp(f'drone-dji:RtkFlag="50" drone-dji:RtkStdLon="{bad}" drone-dji:RtkStdLat="0.01" '
                                    'drone-dji:RtkStdHgt="0.02" drone-dji:RtkDiffAge="nan">'))
        assert rtk["std"] is None and rtk["diff_age"] is None
        assert gnss_apply.accuracy(rtk["status"], rtk["std"]) == pytest.approx([0.01] * 3)


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


def _jpeg(path, xmp: str | None, when: datetime | None = None):
    """A photo with an EXIF position and, with ``when``, the local time the camera wrote."""
    img = Image.fromarray(np.random.default_rng(0).integers(0, 255, (48, 64, 3), dtype=np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (41, 8, 0.0), 3: "E", 4: (14, 15, 0.0), 5: 0, 6: 123.4})
    if when is not None:
        exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
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


def _at(line: str, utc: datetime) -> str:
    """The MRK ``line`` of a shot taken at ``utc``."""
    gps = (utc - dji.GPS_EPOCH).total_seconds() + dji.LEAP_SECONDS
    week, seconds = divmod(gps, 7 * 86400)
    index, _seconds, _week, rest = line.split("\t", 3)
    return f"{index}\t{seconds:.6f}\t[{int(week)}]\t{rest}"


def _timed(lines: str, utcs) -> str:
    """The MRK ``lines`` shot at the times ``utcs``, one per line."""
    return "".join(_at(line, utc) for line, utc in zip(lines.splitlines(keepends=True), utcs, strict=True))


def _flight(folder, names, lines=MRK_LINES, xmp_first=False):
    folder.mkdir(exist_ok=True)
    for i, name in enumerate(names):
        _jpeg(folder / name, _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.004" drone-dji:RtkStdLat="0.004" '
                                  'drone-dji:RtkStdHgt="0.009">') if xmp_first and i == 0 else None)
    (folder / "Timestamp.MRK").write_text(lines)
    return folder


def _snapshot(chunk):
    return {c.label: (c.reference.location, c.reference.accuracy, c.reference.enabled, dict(c.meta.get("gnss") or {}),
                      c.meta.get("reference_crs")) for c in chunk.cameras}


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
                      "unmatched": [], "shots_unmatched": []}

    shots = dji.read_mrk(folder / "Timestamp.MRK")
    for cam, shot in zip(ch.cameras, shots):
        assert cam.meta["reference_crs"] == "EPSG:4979" and cam.reference.enabled
        assert cam.reference.location == pytest.approx(list(dji.camera_position(shot)), abs=1e-12)
        g = cam.meta["gnss"]
        assert (g["source"], g["height"], g["mrk"], g["shot"]) == ("dji_mrk", "ellipsoidal", "Timestamp.MRK",
                                                                   shot.index)
        assert g["utc"] == shot.utc.isoformat() and g["status"] == dji.STATUS[shot.flag]
        # what the line said, to check the position of the camera against it
        assert g["lever_arm_ned_mm"] == list(shot.offset_ned_mm) and g["antenna"] == [shot.lon, shot.lat, shot.ellh]
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
    assert result["shots_unmatched"] == [5]
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


_LINE = "{0}\t0\t[0]\t     0,N\t     0,E\t   150,V\t{1:.8f},Lat\t14.26600000,Lon\t94.350,Ellh\t0.01, 0.01, 0.02\t50,Q\n"


def test_mrk_of_one_flight_in_a_folder_of_two(tmp_path):
    # two flights of a Mavic 3 copied into one folder: the shot numbers start again from 1 and the clock of the camera
    # is the local time (UTC+2 in May); each flight has its MRK, the second one 500 m north of the first
    folder = tmp_path / "DCIM"
    folder.mkdir()
    flights = {"DJI_202405121030_001_Timestamp.MRK": ("20240512103015", datetime(2024, 5, 12, 8, 30, 15, 400000), 41.134),
               "DJI_202405121137_002_Timestamp.MRK": ("20240512113752", datetime(2024, 5, 12, 9, 37, 52, 300000), 41.1385)}
    for mrk, (stamp, start, lat) in flights.items():
        lines = []
        for i in (1, 2, 3):
            utc = start + timedelta(seconds=2 * i)
            _jpeg(folder / f"DJI_{stamp}_{i:04d}_V.JPG", None, when=utc + timedelta(hours=2))
            lines.append(_at(_LINE.format(i, lat), utc.replace(tzinfo=timezone.utc)))
        (folder / mrk).write_text("".join(lines))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    first, second = ch.cameras[:3], ch.cameras[3:]
    assert all("103015" in c.label for c in first) and all("113752" in c.label for c in second)

    # the MRK of the first flight: its photos only, the ones of the second flight have the same shot numbers but
    # were taken an hour later
    result = gnss_apply.apply_mrk(ch, folder / "DJI_202405121030_001_Timestamp.MRK")
    assert (result["matched"], result["shots"], result["photos"]) == (3, 3, 6)
    assert result["unmatched"] == [c.label for c in second] and result["shots_unmatched"] == []
    assert all(c.meta["gnss"]["source"] == "dji_mrk" for c in first)
    assert [c.reference.location[1] for c in first] == pytest.approx([41.134] * 3, abs=1e-9)
    assert all(c.meta["gnss"]["source"] == "exif" for c in second)

    # the MRK of the second flight gives its photos theirs and leaves the first flight as it is
    result = gnss_apply.apply_mrk(ch, folder / "DJI_202405121137_002_Timestamp.MRK")
    assert result["matched"] == 3 and result["unmatched"] == [c.label for c in first]
    assert [c.reference.location[1] for c in second] == pytest.approx([41.1385] * 3, abs=1e-9)
    assert [c.reference.location[1] for c in first] == pytest.approx([41.134] * 3, abs=1e-9)


def test_mrk_of_a_flight_that_lost_photos_beside_the_next_flight(tmp_path):
    # the flight of the MRK lost its fourth photo; the next battery, copied into the same folder, started an hour and
    # 30 s later with the same interval: its four photos fit a time zone too and are more than the three left of the
    # flight of the MRK - the times cannot say which flight the MRK is of, so nothing is paired
    folder = tmp_path / "DCIM"
    folder.mkdir()
    start = datetime(2024, 5, 12, 8, 30, 15, 400000)
    lines = []
    for i in (1, 2, 3, 4):
        utc = start + timedelta(seconds=2 * i)
        if i < 4:
            _jpeg(folder / f"DJI_20240512103015_{i:04d}_V.JPG", None, when=utc + timedelta(hours=2))
        _jpeg(folder / f"DJI_20240512113045_{i:04d}_V.JPG", None, when=utc + timedelta(hours=3, seconds=30))
        lines.append(_at(_LINE.format(i, 41.134), utc.replace(tzinfo=timezone.utc)))
    (folder / "Timestamp.MRK").write_text("".join(lines))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    before = _snapshot(ch)
    with pytest.raises(ValueError, match="more than one flight"):
        gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")
    assert _snapshot(ch) == before

    # the next flight exactly an hour later, as many photos as the flight of the MRK: the same refusal
    for f in folder.glob("DJI_20240512113045_*"):
        f.unlink()
    for i in (1, 2, 3):
        utc = start + timedelta(seconds=2 * i)
        _jpeg(folder / f"DJI_20240512113015_{i:04d}_V.JPG", None, when=utc + timedelta(hours=3))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    with pytest.raises(ValueError, match="more than one flight"):
        gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")


def test_mrk_shots_checked_against_the_time_of_the_photos(tmp_path):
    start = datetime(2024, 5, 12, 8, 30, 15, 400000)
    utcs = [start + timedelta(seconds=2 * i) for i in range(4)]
    local = [t + timedelta(hours=2) for t in utcs]  # the camera on the local time of Italy in May: +02:00
    names = [f"DJI_{i:04d}.JPG" for i in (1, 2, 3, 4)]

    def flight(name, clock):
        folder = tmp_path / name
        folder.mkdir()
        for photo, when in zip(names, clock):
            _jpeg(folder / photo, None, when=when)
        (folder / "Timestamp.MRK").write_text("".join(_at(_LINE.format(i + 1, 41.134), t.replace(tzinfo=timezone.utc))
                                                      for i, t in enumerate(utcs)))
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        return ch, folder / "Timestamp.MRK"

    ch, mrk = flight("zone", local)
    result = gnss_apply.apply_mrk(ch, mrk)
    assert (result["matched"], result["unmatched"], result["shots_unmatched"]) == (4, [], [])

    # a photo taken 10 s away from its shot and one without a date are not paired, and their shots are said
    ch, mrk = flight("off", [local[0], local[1] + timedelta(seconds=10), None, local[3]])
    result = gnss_apply.apply_mrk(ch, mrk)
    assert (result["matched"], result["unmatched"], result["shots_unmatched"]) == (2, ["DJI_0002", "DJI_0003"], [2, 3])
    assert [c.meta["gnss"]["source"] for c in ch.cameras] == ["dji_mrk", "exif", "exif", "dji_mrk"]

    # the photos 7 minutes away from any time zone, or a year away: not the photos of this MRK, nothing changes
    for name, clock in (("late", [t + timedelta(hours=2, minutes=7) for t in utcs]),
                        ("year", [t + timedelta(days=365, hours=2) for t in utcs])):
        ch, mrk = flight(name, clock)
        before = _snapshot(ch)
        with pytest.raises(ValueError, match="times"):
            gnss_apply.apply_mrk(ch, mrk)
        assert _snapshot(ch) == before

    # one photo at the time of its shot, the others each at a time of its own: the MRK of another flight
    ch, mrk = flight("other", [local[0]] + [t + timedelta(seconds=40 * i) for i, t in enumerate(local[1:], 1)])
    before = _snapshot(ch)
    with pytest.raises(ValueError, match="1 of 4"):
        gnss_apply.apply_mrk(ch, mrk)
    assert _snapshot(ch) == before


# --- positions from a file -------------------------------------------------------------------------------------

def _chunk_with(tmp_path, *names):
    """A chunk with photos called ``names`` (each with an EXIF position of its own)."""
    folder = tmp_path / "photos"
    folder.mkdir(exist_ok=True)
    ch = ppg.Document().add_chunk()
    ch.add_photos([_jpeg(folder / n, None) for n in names])
    return ch


def test_positions_file_with_header_and_without(tmp_path):
    ch = _chunk_with(tmp_path, "IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG")
    # the columns are found by name, so latitude may come before longitude; one standard deviation for the three axes
    named = tmp_path / "named.csv"
    named.write_text("name,lat,lon,ellh,std\n"
                     "IMG_0001,41.134,14.266,94.35,0.01\n"
                     "img_0002.jpg,41.135,14.267,94.50,0.02\n"
                     "GONE.JPG,41.136,14.268,94.60,0.01\n")
    rows = positions.read_positions(named)
    assert [r["label"] for r in rows] == ["IMG_0001", "img_0002.jpg", "GONE.JPG"]
    assert (rows[0]["x"], rows[0]["y"], rows[0]["z"]) == (14.266, 41.134, 94.35)
    assert (rows[0]["sx"], rows[0]["sy"], rows[0]["sz"]) == (0.01, 0.01, 0.01)

    result = positions.apply_positions(ch, rows, "EPSG:4979")
    assert result == {"matched": 2, "rows": 3, "unmatched": ["GONE.JPG"]}
    first, second, third = ch.cameras
    assert first.reference.location == [14.266, 41.134, 94.35] and first.reference.enabled
    assert second.reference.location == [14.267, 41.135, 94.5]
    # twice the declared standard deviation, as for the RTK of the photos
    assert first.reference.accuracy == pytest.approx([0.02] * 3)
    assert second.reference.accuracy == pytest.approx([0.04] * 3)
    assert first.meta["reference_crs"] == "EPSG:4979"
    assert first.meta["gnss"] == {"source": "positions_file", "status": None, "accuracy": first.reference.accuracy,
                                  "height": "ellipsoidal", "std": [0.01] * 3}
    assert third.meta["gnss"]["source"] == "exif" and third.meta["reference_crs"] == "EPSG:4326"  # not in the file

    again = _snapshot(ch)
    assert positions.apply_positions(ch, positions.read_positions(named), "EPSG:4979") == result
    assert _snapshot(ch) == again

    # without a header: label, x, y, z, with the default accuracy of a file that has none ...
    bare = tmp_path / "bare.txt"
    bare.write_text("IMG_0001.JPG,500000.5,4500000.25,101.5\nIMG_0002.JPG,500010.5,4500000.25,101.75\n")
    rows = positions.read_positions(bare)
    assert rows == [{"label": "IMG_0001.JPG", "x": 500000.5, "y": 4500000.25, "z": 101.5},
                    {"label": "IMG_0002.JPG", "x": 500010.5, "y": 4500000.25, "z": 101.75}]
    result = positions.apply_positions(ch, rows, "EPSG:32633", height="exif")
    assert result == {"matched": 2, "rows": 2, "unmatched": []}
    # ... which replaces what the first file gave: location, system, accuracy and what the record said
    assert first.reference.location == [500000.5, 4500000.25, 101.5] and first.meta["reference_crs"] == "EPSG:32633"
    assert first.reference.accuracy == [0.05, 0.05, 0.10]
    assert first.meta["gnss"] == {"source": "positions_file", "status": None, "accuracy": [0.05, 0.05, 0.10],
                                  "height": "exif"}
    assert positions.apply_positions(ch, rows, "EPSG:32633", default_accuracy=(0.02, 0.02, 0.04))["matched"] == 2
    assert first.reference.accuracy == [0.02, 0.02, 0.04]

    # ... or label, x, y, z, sx, sy, sz
    seven = tmp_path / "seven.txt"
    seven.write_text("IMG_0003\t500020\t4500000\t102\t0.01\t0.02\t0.05\n")
    positions.apply_positions(ch, positions.read_positions(seven), "EPSG:32633")
    assert third.reference.accuracy == pytest.approx([0.02, 0.04, 0.10])
    assert third.meta["gnss"]["std"] == [0.01, 0.02, 0.05]


def test_positions_file_formats(tmp_path):
    one = {"label": "a.jpg", "x": 14.5, "y": 41.25, "z": 100.5}
    # Excel in Italian: semicolons, decimal commas, a byte order mark, a comment, units in the headers
    p = tmp_path / "it.csv"
    p.write_bytes("\ufeff# survey of the trench\nFoto;Longitudine (deg);Latitudine (deg);Quota [m];Accuracy\n"
                  "\n# a comment\na.jpg;14,5;41,25;100,5;0,03\n".encode("utf-8"))
    (row,) = positions.read_positions(p)
    assert row == {**one, "accuracy": 0.03}
    # tabs, and spaces (the label cannot hold any)
    p = tmp_path / "tabs.tsv"
    p.write_text("easting\tnorthing\theight\timage\tyaw\tpitch\troll\n14.5\t41.25\t100.5\ta.jpg\t90\t-80\t0\n")
    (row,) = positions.read_positions(p)
    assert row == {**one, "yaw": 90.0, "pitch": -80.0, "roll": 0.0}
    p = tmp_path / "spaces.txt"
    p.write_text("a.jpg   14.5  41.25   100.5\n")
    assert positions.read_positions(p) == [one]
    # the header may be the first comment, and a delimiter can be forced
    p = tmp_path / "hash.csv"
    p.write_text("#label|x|y|z\na.jpg|14.5|41.25|100.5\n")
    assert positions.read_positions(p, delimiter="|") == [one]
    # a file with a header of the trimmed name list and a status
    p = tmp_path / "status.csv"
    p.write_text("label,x,y,z,sde,sdn,sdu,q\na.jpg,1,2,3,0.004,0.004,0.009,1\nb.jpg,1,2,3,0.1,0.1,0.2,2\n"
                 "c.jpg,1,2,3,0.5,0.5,0.9,5\nd.jpg,1,2,3,0.5,0.5,0.9,0\ne.jpg,1,2,3,0.5,0.5,0.9,\n")
    rows = positions.read_positions(p)
    assert [r.get("status") for r in rows] == ["fix", "float", "single", "none", None]
    assert (rows[0]["sx"], rows[0]["sy"], rows[0]["sz"]) == (0.004, 0.004, 0.009)


def test_a_tab_delimiter_keeps_its_empty_cells(tmp_path):
    p = tmp_path / "gaps.tsv"
    p.write_text("name\tx\ty\tz\tyaw\tpitch\na.jpg\t1\t2\t3\t\t-80\n")
    expected = {"label": "a.jpg", "x": 1.0, "y": 2.0, "z": 3.0, "pitch": -80.0}  # no yaw: its cell is empty
    assert positions.read_positions(p) == [expected]
    assert positions.read_positions(p, delimiter="\t") == [expected]
    # an empty first cell is an empty name, not the start of the next column
    p.write_text("name\tx\ty\tz\n\t1\t2\t3\n")
    for kw in ({}, {"delimiter": "\t"}):
        with pytest.raises(ValueError, match=r"gaps\.tsv:2.*no name"):
            positions.read_positions(p, **kw)
    # a line of separators alone (the empty rows of a spreadsheet) is skipped; only spaces mean runs of spaces
    p.write_text("name;x;y;z\na.jpg;1;2;3\n;;;\n")
    assert len(positions.read_positions(p)) == 1
    p.write_text("a.jpg   1  2   3\n")
    assert positions.read_positions(p, delimiter=" ") == [{"label": "a.jpg", "x": 1.0, "y": 2.0, "z": 3.0}]


def test_positions_accuracy_follows_the_status(tmp_path):
    ch = _chunk_with(tmp_path, "a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg")
    rows = [
        {"label": "a", "x": 1.0, "y": 2.0, "z": 3.0, "sx": 0.004, "sy": 0.004, "sz": 0.009, "status": "fix"},
        {"label": "b", "x": 1.0, "y": 2.0, "z": 3.0, "sx": 0.05, "sy": 0.05, "sz": 0.05, "status": "float"},
        {"label": "c", "x": 1.0, "y": 2.0, "z": 3.0, "sx": 0.3, "sy": 0.4, "sz": 0.5},  # no status: no floor
        {"label": "d", "x": 1.0, "y": 2.0, "z": 3.0, "accuracy": 0.03, "status": "fix"},  # an accuracy is as given
        {"label": "e", "x": 1.0, "y": 2.0, "z": 3.0, "yaw": 12.0, "pitch": -90.0, "roll": 1.5},
    ]
    positions.apply_positions(ch, rows, "LOCAL")
    a, b, c, d, e = ch.cameras
    assert a.reference.accuracy == pytest.approx([0.01, 0.01, 0.018])
    assert b.reference.accuracy == pytest.approx([0.2] * 3)
    assert c.reference.accuracy == pytest.approx([0.6, 0.8, 1.0])
    assert d.reference.accuracy == pytest.approx([0.03] * 3) and "std" not in d.meta["gnss"]
    assert e.reference.accuracy == [0.05, 0.05, 0.10]
    assert (a.meta["gnss"]["status"], d.meta["gnss"]["status"], e.meta["gnss"]["status"]) == ("fix", "fix", None)
    assert (e.meta["gnss"]["yaw"], e.meta["gnss"]["pitch"], e.meta["gnss"]["roll"]) == (12.0, -90.0, 1.5)
    assert e.meta["reference_crs"] == "LOCAL"


def test_positions_status_floors_the_default_accuracy(tmp_path):
    ch = _chunk_with(tmp_path, "a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "f.jpg")
    where = {"x": 1.0, "y": 2.0, "z": 3.0}
    rows = [{"label": name, "status": status, **where} for name, status in
            zip("abcdef", ("fix", "float", "single", "none", "unknown", None))]
    positions.apply_positions(ch, rows, "LOCAL")
    fix, flt, single, none, unknown, plain = (c.reference.accuracy for c in ch.cameras)
    assert fix == [0.05, 0.05, 0.10]  # the default is already above the floor of a fix
    assert flt == pytest.approx([0.2] * 3) and single == pytest.approx([1.0] * 3)
    assert none == [5.0, 5.0, 10.0] and unknown == pytest.approx([1.0] * 3)  # as for the RTK of the photos
    assert plain == [0.05, 0.05, 0.10]
    # a default above the floor stays, axis by axis
    positions.apply_positions(ch, rows, "LOCAL", default_accuracy=(0.1, 0.5, 0.3))
    fix, flt, single, none, unknown, plain = (c.reference.accuracy for c in ch.cameras)
    assert fix == [0.1, 0.5, 0.3] and flt == pytest.approx([0.2, 0.5, 0.3]) and plain == [0.1, 0.5, 0.3]
    assert single == pytest.approx([1.0] * 3) and none == [5.0, 5.0, 10.0]
    assert [c.meta["gnss"]["accuracy"] for c in ch.cameras] == [c.reference.accuracy for c in ch.cameras]


def test_positions_matched_by_name_and_folder(tmp_path):
    ch = ppg.Document().add_chunk()
    for folder in ("100MEDIA", "101MEDIA"):
        (tmp_path / folder).mkdir()
        ch.add_photos([_jpeg(tmp_path / folder / "DJI_0001.JPG", None)])
    ch.add_photos([_jpeg(tmp_path / "100MEDIA" / "DJI_0002.JPG", None)])
    ch.add_photos([_jpeg(tmp_path / "100MEDIA" / "DJI_0002.png", None)])
    before = _snapshot(ch)
    row = {"x": 1.0, "y": 2.0, "z": 3.0}

    # the same name in two folders, or a name without the extension of two files: refused, nothing changes
    with pytest.raises(ValueError, match="DJI_0001.JPG.*2 photos"):
        positions.apply_positions(ch, [{"label": "DJI_0001.JPG", **row}, {"label": "DJI_0002.jpg", **row}], "LOCAL")
    with pytest.raises(ValueError, match="DJI_0002.*2 photos"):
        positions.apply_positions(ch, [{"label": "DJI_0002", **row}], "LOCAL")
    assert _snapshot(ch) == before
    # two rows for one photo
    with pytest.raises(ValueError, match="twice"):
        positions.apply_positions(ch, [{"label": "DJI_0002.jpg", **row}, {"label": "DJI_0002.JPG", **row}], "LOCAL")
    assert _snapshot(ch) == before

    # the folder tells them apart, with either kind of slash and with or without the extension
    result = positions.apply_positions(ch, [{"label": "101MEDIA/DJI_0001", **row},
                                            {"label": "100MEDIA\\DJI_0001.jpg", "x": 5.0, "y": 6.0, "z": 7.0},
                                            {"label": str((tmp_path / "100MEDIA" / "DJI_0002.png").resolve()), **row},
                                            {"label": "elsewhere/DJI_0001.JPG", **row}], "LOCAL")
    assert result == {"matched": 3, "rows": 4, "unmatched": ["elsewhere/DJI_0001.JPG"]}
    first, second, third, fourth = ch.cameras
    assert second.reference.location == [1.0, 2.0, 3.0] and first.reference.location == [5.0, 6.0, 7.0]
    assert fourth.meta["gnss"]["source"] == "positions_file" and third.meta["gnss"]["source"] == "exif"


def test_positions_matched_by_a_windows_path():
    # the drive of a Windows path is "C:\\" among its parts and "c:" in a label split on the slashes; a share is one part
    drive = PureWindowsPath(r"C:\Users\me\100MEDIA\DJI_0002.png")
    share = PureWindowsPath(r"\\server\rilievi\100MEDIA\DJI_0003.JPG")
    assert positions._parts(drive) == ("c:", "users", "me", "100media", "dji_0002.png")
    assert positions._parts(share) == ("server", "rilievi", "100media", "dji_0003.jpg")
    index = ({}, {})
    for cam, path in (("a", drive), ("b", share)):
        parts = positions._parts(path)
        index[0].setdefault(parts[-1], []).append((cam, parts))
        index[1].setdefault(PureWindowsPath(parts[-1]).stem, []).append((cam, parts))
    assert positions._find(index, str(drive)) == ["a"]
    assert positions._find(index, "C:/Users/me/100MEDIA/DJI_0002") == ["a"]
    assert positions._find(index, str(share)) == ["b"]
    assert positions._find(index, r"D:\Users\me\100MEDIA\DJI_0002.png") == []


def test_positions_files_that_cannot_be_used(tmp_path):
    def read(text, **kw):
        p = tmp_path / "f.csv"
        p.write_text(text)
        return positions.read_positions(p, **kw)

    with pytest.raises(ValueError, match="no positions"):
        read("# nothing\n\n")
    with pytest.raises(ValueError, match="height"):
        read("name,lat,lon\na.jpg,41,14\n")
    with pytest.raises(ValueError, match="name"):
        read("lat,lon,ellh\n41,14,90\n")
    with pytest.raises(ValueError, match="sx, sy and sz"):
        read("name,lat,lon,ellh,sx,sy\na.jpg,41,14,90,0.1,0.1\n")
    with pytest.raises(ValueError, match="5 columns"):
        read("a.jpg,41,14,90,0.5\n")  # label, x, y, z and a fifth column nobody named
    with pytest.raises(ValueError, match=r"f\.csv:3.*'abc'"):
        read("name,lat,lon,ellh\na.jpg,41,14,90\nb.jpg,abc,14,90\n")
    with pytest.raises(ValueError, match=r"f\.csv:2"):
        read("name,lat,lon,ellh\na.jpg,41,nan,90\n")
    with pytest.raises(ValueError, match="accuracy"):
        read("name,lat,lon,ellh,std\na.jpg,41,14,90,0\n")
    with pytest.raises(ValueError, match=r"f\.csv:2.*sy"):
        read("name,lat,lon,ellh,sx,sy,sz\na.jpg,41,14,90,0.1,,0.1\n")
    with pytest.raises(ValueError, match=r"f\.csv:3.*status"):
        read("name,lat,lon,ellh,status\na.jpg,41,14,90,50\nb.jpg,41,14,90,inf\n")

    ch = _chunk_with(tmp_path, "a.jpg")
    good = [{"label": "a", "x": 14.0, "y": 41.0, "z": 90.0}]
    before = _snapshot(ch)
    with pytest.raises(ValueError, match="latitude"):  # longitude and latitude swapped
        positions.apply_positions(ch, [{"label": "a", "x": 41.0, "y": 114.0, "z": 90.0}], "EPSG:4979")
    with pytest.raises(ValueError, match="height"):
        positions.apply_positions(ch, good, "EPSG:4979", height="geoid")
    with pytest.raises(Exception, match="coordinate system"):
        positions.apply_positions(ch, good, "EPSG:no")
    assert _snapshot(ch) == before


# --- the positions of the photos summed up per flight ----------------------------------------------------------

def _camera(key, when, status=None, source="dji_xmp", accuracy=None, position=True, sensor=0):
    """A camera shot at ``when`` (EXIF date text), with a position of ``status`` (``False``: none at all)."""
    cam = ppg.core.camera.Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=sensor,
                                 meta={"datetime": when})
    if position:
        gnss_apply.set_camera_position(cam, [500000.0 + key, 4500000.0, 100.0], "EPSG:32633",
                                       accuracy or gnss_apply.accuracy(status, None), source, status=status)
    return cam


def test_summary_per_flight(monkeypatch):
    ch = ppg.Document().add_chunk()
    ch.cameras = [
        # first flight: 10:00-10:01, one RTK fix, one photo with a GPS but no RTK data, one without a position
        _camera(0, "2026:10:26 10:00:00", "fix", accuracy=[0.01, 0.02, 0.03]),
        _camera(1, "2026:10:26 10:00:30", None, source="exif"),
        _camera(2, "2026:10:26 10:01:00", position=False),
        # second flight, 30 minutes later: two fixes (one with its reference disabled), a float, a status DJI does
        # not know, and a photo with no date at all (flight 0)
        _camera(3, "2026:10:26 10:31:00", "fix", source="dji_mrk", accuracy=[0.01, 0.01, 0.01]),
        _camera(4, "2026:10:26 10:31:30", "fix", source="dji_mrk", accuracy=[0.03, 0.01, 0.05]),
        _camera(5, "2026:10:26 10:32:00", "float", source="positions_file", accuracy=[0.2, 0.2, 0.2]),
        _camera(6, "2026:10:26 10:32:30", "unknown", source="positions_file", accuracy=[1.0, 1.0, 1.0]),
        _camera(7, "", "fix", accuracy=[0.01, 0.01, 0.02]),
    ]
    ch.cameras[4].reference.enabled = False
    monkeypatch.setattr(ppg.markers.gcp, "camera_reference_errors", lambda chunk: {})

    nodate, one, two = quality.summary(ch)  # by flight number, the photos with no date (flight 0) first
    assert [nodate["flight"], one["flight"], two["flight"]] == [0, 1, 2]

    assert one["photos"] == 3 and one["status"] == {"fix": 1, "float": 0, "single": 0, "none": 1}
    assert one["sources"] == {"dji_xmp": 1, "exif": 1}
    assert one["no_position"] == 1 and one["disabled"] == 0
    # the median of the accuracy of the two photos that have one: the fix and the one with no RTK data
    assert one["accuracy_median"] == pytest.approx([2.505, 2.51, 5.015])
    assert one["residual_rms"] is None and one["residual_max"] is None  # nothing aligned and georeferenced

    assert two["photos"] == 4 and two["status"] == {"fix": 2, "float": 1, "single": 1, "none": 0}  # unknown = single
    assert two["sources"] == {"dji_mrk": 2, "positions_file": 2}
    assert two["no_position"] == 0 and two["disabled"] == 1
    assert two["accuracy_median"] == pytest.approx([0.115, 0.105, 0.125])

    assert nodate["photos"] == 1 and nodate["status"]["fix"] == 1
    assert nodate["accuracy_median"] == pytest.approx([0.01, 0.01, 0.02])


def test_kinds_of_height_per_flight_and_mixed(monkeypatch):
    monkeypatch.setattr(ppg.markers.gcp, "camera_reference_errors", lambda chunk: {})
    ch = ppg.Document().add_chunk()
    ch.cameras = [_camera(0, "2026:10:26 10:00:00", "fix", accuracy=[0.01, 0.01, 0.02]),  # the RTK of the XMP
                  _camera(1, "2026:10:26 10:00:30", None, source="exif"),
                  _camera(2, "2026:10:26 10:01:00", position=False)]
    (one,) = quality.summary(ch)
    assert one["heights"] == {"exif": 2}  # the photos with a position
    assert quality.mixed_heights(ch) == []

    # an ellipsoidal height of 10 m accuracy beside a height of the EXIF: not enough to matter
    gnss_apply.set_camera_position(ch.cameras[1], [14.0, 41.0, 140.0], "EPSG:4979", [5.0, 5.0, 10.0], "dji_mrk",
                                   status="none", height="ellipsoidal")
    (one,) = quality.summary(ch)
    assert one["heights"] == {"exif": 1, "ellipsoidal": 1}
    assert quality.mixed_heights(ch) == []
    # both accurate in height: heights of two kinds pull the block apart
    ch.cameras[1].reference.accuracy = [0.02, 0.02, 0.04]
    assert quality.mixed_heights(ch) == ["ellipsoidal", "exif"]
    ch.cameras[0].reference.enabled = False  # not if one of them is switched off
    assert quality.mixed_heights(ch) == []
    ch.cameras[0].reference.enabled = True

    # a position from somewhere that does not say its kind of height
    ch.cameras[2].reference = ppg.core.camera.Reference(location=[500000.0, 4500000.0, 90.0], accuracy=[0.01] * 3,
                                                        enabled=True)
    (one,) = quality.summary(ch)
    assert one["heights"] == {"exif": 1, "ellipsoidal": 1, "unknown": 1}
    assert quality.mixed_heights(ch) == ["ellipsoidal", "exif", "unknown"]


def test_summary_residuals_of_the_aligned_cameras(monkeypatch):
    ch = ppg.Document().add_chunk()
    ch.cameras = [_camera(k, f"2026:10:26 1{k // 2}:00:00", "fix") for k in range(4)]  # k // 2: two flights
    deltas = {0: [0.03, 0.0, 0.0], 1: [0.0, 0.04, 0.0], 2: [0.0, 0.0, 0.12], 3: [0.03, 0.04, 0.0]}
    errors = {k: {"reference": np.zeros(3), "estimated": np.array(d), "delta": np.array(d),
                  "error": float(np.linalg.norm(d))} for k, d in deltas.items() if k != 3}
    errors[3] = {"reference": np.zeros(3)}  # a camera that is not aligned has a reference and nothing else
    monkeypatch.setattr(ppg.markers.gcp, "camera_reference_errors", lambda chunk: errors)

    one, two = quality.summary(ch)
    assert one["residual_rms"] == pytest.approx([np.sqrt(0.03 ** 2 / 2), np.sqrt(0.04 ** 2 / 2), 0.0])
    assert one["residual_max"] == pytest.approx(0.04)
    assert two["residual_rms"] == pytest.approx([0.0, 0.0, 0.12])  # the camera 3 is not in it
    assert two["residual_max"] == pytest.approx(0.12)


def test_summary_of_a_georeferenced_chunk_and_of_an_empty_one():
    assert quality.summary(ppg.Document().add_chunk()) == []

    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:32633"
    ch.cameras = [_camera(k, f"2026:10:26 10:0{k}:00", "fix") for k in range(2)]
    for cam in ch.cameras:  # the camera 0 is 3 cm east of its reference, the camera 1 is not aligned
        cam.transform = np.eye(4)
        cam.transform[:3, 3] = cam.reference.location
    ch.cameras[0].transform[0, 3] += 0.03
    ch.cameras[1].transform = None
    (entry,) = quality.summary(ch)
    assert entry["residual_rms"] == pytest.approx([0.03, 0.0, 0.0], abs=1e-6)
    assert entry["residual_max"] == pytest.approx(0.03, abs=1e-6)


# --- positions in several coordinate systems in one chunk ------------------------------------------------------

def _mixed(first: str, span: float = 2e-4):
    """Six aligned photos over one place, their positions alternately the GPS of the EXIF (EPSG:4326) and a file in
    UTM 33N (EPSG:32633), the first in ``first``; and where they truly are in UTM 33N."""
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32633", always_xy=True)
    other = "EPSG:32633" if first == "EPSG:4326" else "EPSG:4326"
    ch = ppg.Document().add_chunk()
    truth = []
    for i in range(6):
        lon, lat, h = 14.266 + span * i, 41.134 + span * (i % 3), 120.0 + i
        e, n = to_utm.transform(lon, lat)
        truth.append([e, n, h])
        cam = ppg.core.camera.Camera(key=i, label=f"p{i}", path=f"p{i}.jpg", sensor_key=0)
        crs = first if i % 2 == 0 else other
        location = [lon, lat, h] if crs == "EPSG:4326" else [e, n, h]
        gnss_apply.set_camera_position(cam, location, crs, [1.0, 1.0, 2.0], "exif" if crs == "EPSG:4326" else
                                       "positions_file")
        cam.transform = np.eye(4)
        ch.cameras.append(cam)
    return ch, np.array(truth)


@pytest.mark.parametrize("first", ["EPSG:4326", "EPSG:32633"])
def test_positions_in_several_systems_on_one_metric_frame(tmp_path, first):
    import pycolmap

    from pyarchinit_photogrammetry.engine import sfm
    from pyarchinit_photogrammetry.markers import gcp

    ch, truth = _mixed(first)
    names = {c.key: f"{c.label}.jpg" for c in ch.cameras}

    # the preselection of the pairs by the positions of the photos: all on the same metric axes
    positions = sfm._reference_positions(ch, names)
    assert np.array([positions[names[k]] for k in range(6)]) == pytest.approx(truth, abs=1e-3)

    # the positions holding the block in the alignment
    db_path = tmp_path / "database.db"
    db = pycolmap.Database.open(str(db_path))
    try:
        camera = db.write_camera(pycolmap.Camera.create_from_model_id(0, pycolmap.CameraModelId.SIMPLE_PINHOLE,
                                                                      100.0, 64, 48))
        for name in names.values():
            db.write_image(pycolmap.Image(name=name, camera_id=camera))
    finally:
        db.close()
    report = sfm._write_pose_priors(ch, db_path, names)
    assert report["count"] == 6 and report["crs"] == "EPSG:32633"
    db = pycolmap.Database.open(str(db_path))
    try:
        by_image = {image.image_id: image.name for image in db.read_all_images()}
        priors = {by_image[p.corr_data_id.id]: np.asarray(p.position) for p in db.read_all_pose_priors()}
    finally:
        db.close()
    assert np.array([priors[names[k]] for k in range(6)]) + report["offset"] == pytest.approx(truth, abs=1e-3)

    # how far apart the photos are, in metres, against their accuracy (1 m): a survey or an object on a table
    span = float(np.linalg.norm(np.ptp(truth[:, :2], axis=0)))
    assert gcp.cameras_span_their_accuracy(ch, factor=0.9 * span)
    assert not gcp.cameras_span_their_accuracy(ch, factor=1.1 * span)


# --- ppg gnss and the GNSS pane --------------------------------------------------------------------------------

def _project(tmp_path, names=("a.JPG", "b.JPG", "c.JPG")):
    """A saved project with the photos of ``names`` (with their EXIF position and the MRK beside them; names with
    no shot number, so the MRK is paired by order)."""
    folder = _flight(tmp_path / "100MEDIA", list(names))
    doc = ppg.Document()
    doc.add_chunk("Flight").add_photos([folder])
    doc.save(tmp_path / "flight.ppg")
    return tmp_path / "flight.ppg", folder


def _rows(output: str) -> dict:
    """The lines of ``ppg gnss status`` that start with a flight: flight -> the cells of the line."""
    return {line.split()[0]: line.split() for line in output.splitlines()
            if line.split() and (line.split()[0].isdigit() or line.split()[0] == "undated")}


def test_gnss_command_line(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    project, folder = _project(tmp_path)
    run = CliRunner().invoke

    # the photos only have the GPS of their EXIF: one flight (they have no date), no RTK data, 5/5/10 m
    result = run(app, ["gnss", "status", str(project)])
    assert result.exit_code == 0, result.output
    assert "Flight: 3 photos, 3 with a position" in result.output and "exif 3" in result.output
    assert _rows(result.output)["undated"][:9] == ["undated", "3", "0", "0", "0", "3", "0", "0", "5.000/5.000/10.000"]

    # the MRK gives the three shots their position: the fix, the float and the single of its lines
    result = run(app, ["gnss", "mrk", str(project), str(folder / "Timestamp.MRK")])
    assert result.exit_code == 0, result.output
    assert "3 of 3 photos" in result.output and "fix 1" in result.output and "single 1" in result.output
    cams = ppg.Document.open(project).chunk.cameras
    assert [c.meta["gnss"]["source"] for c in cams] == ["dji_mrk"] * 3
    assert [c.meta["gnss"]["status"] for c in cams] == ["fix", "single", "float"]
    result = run(app, ["gnss", "status", str(project)])
    row = _rows(result.output)["undated"]
    assert row[:8] == ["undated", "3", "1", "1", "1", "0", "0", "0"] and row[-1] == "ellipsoidal:3"
    assert "different kinds" not in result.output

    # a positions file in a projected system, for the photos it names
    table = tmp_path / "positions.csv"
    table.write_text("name,x,y,z,sx,sy,sz\na,500000.5,4500000.25,101.5,0.01,0.01,0.02\n"
                     "gone,500010,4500000,101,0.01,0.01,0.02\n")
    result = run(app, ["gnss", "positions", str(project), str(table), "--crs", "EPSG:32633", "--height", "exif"])
    assert result.exit_code == 0, result.output
    assert "1 of 2 rows" in result.output and "gone" in result.output
    first = ppg.Document.open(project).chunk.cameras[0]
    assert first.reference.location == [500000.5, 4500000.25, 101.5]
    assert first.meta["reference_crs"] == "EPSG:32633" and first.meta["gnss"]["height"] == "exif"
    # its height of the EXIF and the ellipsoidal one of the float, both under a metre: said
    result = run(app, ["gnss", "status", str(project)])
    assert _rows(result.output)["undated"][-1] == "ellipsoidal:2,exif:1"
    assert "the heights of these positions are of different kinds (ellipsoidal / exif)" in result.output

    # what cannot be used is said and nothing changes
    table.write_text("name,x,y\na,1,2\n")
    result = run(app, ["gnss", "positions", str(project), str(table), "--crs", "EPSG:32633"])
    assert result.exit_code == 1 and "error:" in result.output and "height" in result.output
    result = run(app, ["gnss", "positions", str(project), str(table), "--crs", "EPSG:32633", "--height", "geoid"])
    assert result.exit_code != 0
    (folder / "Timestamp.MRK").write_text(MRK_LINES + _numbered(MRK_LINES.splitlines(keepends=True)[0], 4))
    result = run(app, ["gnss", "mrk", str(project), str(folder / "Timestamp.MRK")])
    assert result.exit_code == 1 and "error:" in result.output and "4 shots" in result.output
    assert ppg.Document.open(project).chunk.cameras[0].meta["reference_crs"] == "EPSG:32633"


@pytest.fixture
def gnss_window(tmp_path):
    """A window with a chunk of three photos with the position of an MRK (fix, single, float), the first two
    shot at 10:00 and the third an hour later: two flights."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    times = [datetime(2026, 10, 26, 10, 0, 0), datetime(2026, 10, 26, 10, 0, 30), datetime(2026, 10, 26, 11, 30, 0)]
    # the clock of the camera on the time of Italy (+01:00), the shots of the MRK in UTC
    folder = _flight(tmp_path / "100MEDIA", ["a.JPG", "b.JPG", "c.JPG"],
                     lines=_timed(MRK_LINES, [(t - timedelta(hours=1)).replace(tzinfo=timezone.utc) for t in times]))
    doc = ppg.Document()
    chunk = doc.add_chunk("Flight")
    chunk.add_photos([folder])
    for cam, when in zip(chunk.cameras, times):
        cam.meta["datetime"] = when.strftime("%Y:%m:%d %H:%M:%S")
    gnss_apply.apply_mrk(chunk, folder / "Timestamp.MRK")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    yield w, chunk, folder, mw
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def _cells(table, row):
    return [table.item(row, c).text() if table.item(row, c) else "" for c in range(table.columnCount())]


def test_gnss_pane_shows_the_flights(gnss_window):
    w, chunk, folder, mw = gnss_window
    pane = w.gnss_pane
    assert w.panes.dock(pane).objectName() == "pane_gnss"
    assert pane.table.rowCount() == 2  # one row per flight
    fix, single, flt = chunk.cameras
    heads = [pane.table.horizontalHeaderItem(c).text() for c in range(pane.table.columnCount())]
    column = {name: heads.index(name) for name in ("Volo", "Foto", "Fix", "Float", "Singola", "Senza RTK")}
    one, two = _cells(pane.table, 0), _cells(pane.table, 1)
    assert [one[column[n]] for n in ("Volo", "Foto", "Fix", "Float", "Singola", "Senza RTK")] == ["1", "2", "1", "0", "1", "0"]
    assert [two[column[n]] for n in ("Volo", "Foto", "Fix", "Float", "Singola", "Senza RTK")] == ["2", "1", "0", "1", "0", "0"]
    assert "3" in pane.states["positions"].text() and "MRK" in pane.states["positions"].text()

    # only the fix: the references of the others are switched off, the photos stay
    assert not pane.c_only_fix.isChecked() and [c.reference.enabled for c in chunk.cameras] == [True] * 3
    flt.reference.enabled = False  # one the user switched off by hand
    pane.c_only_fix.click()
    assert [c.reference.enabled for c in chunk.cameras] == [True, False, False]
    assert chunk.meta["gnss_only_fix"]["disabled"] == [single.key]
    assert w.isWindowModified()
    assert _cells(pane.table, 0)[heads.index("Disattivate")] == "1"
    pane.c_only_fix.click()  # and again: only the ones it switched off come back
    assert [c.reference.enabled for c in chunk.cameras] == [True, True, False]
    assert "gnss_only_fix" not in chunk.meta and not pane.c_only_fix.isChecked()

    # a photo without a position is left alone, and the filter is kept when positions are applied again
    nowhere = ppg.core.camera.Camera(key=99, label="nowhere", path=str(folder / "nowhere.jpg"), sensor_key=0)
    chunk.cameras.append(nowhere)
    w.gnss_only_fix(True)
    assert nowhere.reference.location is None and chunk.meta["gnss_only_fix"]["disabled"] == [single.key]
    assert not single.reference.enabled and pane.c_only_fix.isChecked()
    chunk.cameras.remove(nowhere)
    w.gnss_only_fix(False)
    assert single.reference.enabled

    # the kinds of height per flight; heights of two kinds, both under a metre, are said under the positions
    assert _cells(pane.table, 0)[heads.index("Tipo di quota")] == "ellissoidica 2"
    assert "tipo diverso" not in pane.states["positions"].text()
    gnss_apply.set_camera_position(flt, [14.0, 41.0, 90.0], "EPSG:4326", [0.02, 0.02, 0.04], "dji_xmp", status="fix")
    pane.set_state(chunk)
    assert _cells(pane.table, 1)[heads.index("Tipo di quota")] == "EXIF 1"
    assert "Le quote di queste posizioni sono di tipo diverso (ellissoidica / EXIF)" in pane.states["positions"].text()


def test_gnss_window_actions(gnss_window, monkeypatch):
    w, chunk, folder, mw = gnss_window
    pane = w.gnss_pane
    said = []
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_exc", lambda prefix, exc: said.append(("error", f"{prefix}: {exc}")))
    asked = {}
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda parent, title, *args, **kwargs: (asked["file"], ""))

    # an MRK of four shots for three photos cannot be paired by order: said in a message, nothing changes
    mrk = folder / "Timestamp.MRK"
    lines = mrk.read_text()
    (folder / "bad.MRK").write_text(lines + _numbered(lines.splitlines(keepends=True)[0], 4))
    asked["file"] = str(folder / "bad.MRK")
    before = _snapshot(chunk)
    pane.mrk_requested.emit()
    assert said and said[-1][0] == "error" and "4 shots" in said[-1][1]
    assert _snapshot(chunk) == before

    # the right one: the photos take the positions of their shots, the pane shows it (the references back on)
    for cam in chunk.cameras:
        cam.reference.location, cam.meta["gnss"] = None, {}
    asked["file"] = str(mrk)
    w.setWindowModified(False)
    w.gnss_apply_mrk()
    assert [c.meta["gnss"]["source"] for c in chunk.cameras] == ["dji_mrk"] * 3
    assert w.isWindowModified()
    assert _cells(pane.table, 0)[4] == "1" and _cells(pane.table, 1)[3] == "1"  # single, float

    # with only the fix positions on, positions applied again do not bring the others back
    w.gnss_only_fix(True)
    assert [c.reference.enabled for c in chunk.cameras] == [True, False, False]
    w.gnss_apply_mrk()
    assert [c.reference.enabled for c in chunk.cameras] == [True, False, False] and pane.c_only_fix.isChecked()
    w.gnss_only_fix(False)
    assert [c.reference.enabled for c in chunk.cameras] == [True, True, True]

    # a positions file: the dialog of the system and of the kind of height, the default system EPSG:4979
    table = folder / "positions.csv"
    table.write_text("name,lon,lat,h\na,14.1,41.1,90.5\nb,14.2,41.2,91.5\n")
    asked["file"] = str(table)
    fields = {}

    def ask(cls, parent, title, field_list, intro=""):
        fields.update({f.name: f for f in field_list})
        return {"crs": "EPSG:4979", "height": "ellipsoidal"}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    pane.positions_requested.emit()
    assert fields["crs"].default == "EPSG:4979" and fields["height"].default == "ellipsoidal"
    assert [v for v, _label in fields["height"].choices] == ["ellipsoidal", "exif"]
    first, second, third = chunk.cameras
    assert first.reference.location == [14.1, 41.1, 90.5] and first.meta["gnss"]["source"] == "positions_file"
    assert second.reference.accuracy == [0.05, 0.05, 0.10]  # a file that gives none
    assert third.meta["gnss"]["source"] == "dji_mrk"


def test_gnss_mrk_copied_away_from_its_photos(gnss_window, monkeypatch, tmp_path):
    w, chunk, folder, mw = gnss_window
    said = []
    monkeypatch.setattr(w, "_warn", lambda text: said.append(text))
    card = tmp_path / "card"
    card.mkdir()
    (card / "Timestamp.MRK").write_text((folder / "Timestamp.MRK").read_text())
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(card / "Timestamp.MRK"), ""))
    for cam in chunk.cameras:
        cam.reference.location, cam.meta["gnss"] = None, {}

    # no photo of the chunk is beside the MRK: the window asks where they are (and does nothing if it is refused)
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    w.gnss_apply_mrk()
    assert all(c.reference.location is None for c in chunk.cameras)
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    w.gnss_apply_mrk()
    assert [c.meta["gnss"]["source"] for c in chunk.cameras] == ["dji_mrk"] * 3 and not said

    # a folder whose photos are not in the chunk: said, and nothing marked as changed
    other = _flight(tmp_path / "other", ["x.JPG", "y.JPG", "z.JPG"])
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(other))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(other / "Timestamp.MRK"), ""))
    w.setWindowModified(False)
    w.gnss_apply_mrk()
    assert said and "MRK" in said[-1] and not w.isWindowModified()

    # the menu has the three commands
    menu = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Strumenti")
    gnss = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == "GNSS")
    assert [a.text() for a in gnss.actions()] == ["File MRK di DJI...", "File di posizioni...", "Pannello GNSS"]


def test_only_fix_decides_for_the_photos_added_later(tmp_path):
    rtk = _jpeg(tmp_path / "rtk.jpg", _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.004" '
                                          'drone-dji:RtkStdLat="0.004" drone-dji:RtkStdHgt="0.009">'))
    plain = _jpeg(tmp_path / "plain.jpg", None)
    ch = ppg.Document().add_chunk()
    (fix,) = ch.add_photos([rtk])
    assert gnss_apply.only_fix(ch) == 0
    (added,) = ch.add_photos([plain])  # the GPS of the EXIF, no RTK: off at once
    assert not added.reference.enabled and fix.reference.enabled
    assert ch.meta["gnss_only_fix"]["disabled"] == [added.key]
    assert gnss_apply.only_fix(ch, False) == 1 and added.reference.enabled


def test_only_fix_is_kept_when_positions_are_applied_again(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    project, folder = _project(tmp_path)
    doc = ppg.Document.open(project)
    chunk = doc.chunk
    gnss_apply.apply_mrk(chunk, folder / "Timestamp.MRK")  # a fix, a single position, a float
    chunk.cameras[2].reference.enabled = False  # switched off by hand
    assert gnss_apply.only_fix(chunk) == 1  # the single: the float was off already
    assert [c.reference.enabled for c in chunk.cameras] == [True, False, False]
    assert chunk.meta["gnss_only_fix"] == {"disabled": [chunk.cameras[1].key]}
    doc.save()

    # the MRK again from the command line: its positions switch every reference on, the choice decides again
    result = CliRunner().invoke(app, ["gnss", "mrk", str(project), str(folder / "Timestamp.MRK")])
    assert result.exit_code == 0, result.output
    chunk = ppg.Document.open(project).chunk
    assert [c.reference.enabled for c in chunk.cameras] == [True, False, False]
    assert chunk.meta["gnss_only_fix"] == {"disabled": [chunk.cameras[1].key, chunk.cameras[2].key]}
    assert "only fix: the references of 2 photos are switched off" in CliRunner().invoke(
        app, ["gnss", "status", str(project)]).output

    # off: the ones it switched off come back
    assert gnss_apply.only_fix(chunk, False) == 2 and "gnss_only_fix" not in chunk.meta
    assert [c.reference.enabled for c in chunk.cameras] == [True] * 3
    assert gnss_apply.only_fix(chunk, False) == 0  # nothing to give back


def _fake_rnx2rtkp(folder: Path) -> Path:
    """A stand-in for RTKLIB-EX's rnx2rtkp: writes its arguments into the -o file and, as rnx2rtkp
    does, its messages to stderr separated by carriage returns. As RTKLIB-EX, a rover without
    observations (noobs.obs) is "error : no obs data" with exit status 0 and no output; a run.log
    beside it lists every run; old.obs exits 255 as RTKLIB 2.4.3 does for -1."""
    script = folder / "rnx2rtkp"
    log = folder / "run.log"
    script.write_text(f"""#!{sys.executable}
import sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
open({str(log)!r}, "a").write(" ".join(args) + "\\n")
sys.stderr.write("reading    : 2005/04/02 00:00:00.0\\r")
if any(a.endswith("noobs.obs") for a in args):
    sys.stderr.write("error : no obs data\\r" + " " * 40 + "\\r")
    sys.exit(0)
if any(a.endswith("old.obs") for a in args):
    sys.stderr.write("error : no sat ant pcv in igs.atx\\r")
    sys.exit(255)
open(out, "w").write(" ".join(args))
sys.stderr.write("processing : 2005/04/02 00:59:30.0 Q=1\\r" + " " * 40 + "\\r")
""")
    script.chmod(0o755)
    if os.name == "nt":  # no shebang lines on Windows: a batch file starts the script
        wrapper = folder / "rnx2rtkp.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return wrapper
    return script


def test_ppk_without_the_core_says_what_to_do(tmp_path, monkeypatch):
    """Without RTKLIB in the native core (missing, older than 0.5 or switched off) the PPK runs an
    rnx2rtkp named by PPG_RNX2RTKP, and without that says what to install."""
    monkeypatch.setenv("PPG_NATIVE", "0")  # as without the core
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    assert not _native.supports("ppk")
    conf, out = tmp_path / "ppk.conf", tmp_path / "rover.pos"
    conf.write_text("pos1-posmode =kinematic\n")
    files = [str(tmp_path / name) for name in ("rover.obs", "base.obs", "base.nav")]
    with pytest.raises(RuntimeError) as error:
        _native.ppk_solve(str(conf), files, str(out))
    text = str(error.value)
    assert "pyarchinit-photogrammetry-core" in text and "0.5" in text and "PPG_RNX2RTKP" in text and "rnx2rtkp" in text
    # only RTKLIB-EX: RTKLIB 2.4.3 handles the antenna of the base and some options otherwise
    assert "RTKLIB-EX 2.5" in text and "2.4.3" not in text
    # an rnx2rtkp named by PPG_RNX2RTKP: rnx2rtkp -k options -o output files, its code and last message
    monkeypatch.setenv("PPG_RNX2RTKP", str(_fake_rnx2rtkp(tmp_path)))
    code, message = _native.ppk_solve(str(conf), files, str(out))
    assert code == 0 and message.startswith("processing : 2005/04/02 00:59:30.0")
    assert out.read_text() == " ".join(["-k", str(conf), "-o", str(out)] + files)
    # nothing computed is exit status 0 with RTKLIB's error, as in the core
    nothing = tmp_path / "nothing.pos"
    code, message = _native.ppk_solve(str(conf), [str(tmp_path / "noobs.obs")] + files[1:], str(nothing))
    assert code == 0 and message == "error : no obs data" and not nothing.exists()
    # any other exit status is 1 (RTKLIB 2.4.3 exits with postpos's -1, 255 or 4294967295)
    code, message = _native.ppk_solve(str(conf), [str(tmp_path / "old.obs")] + files[1:], str(out))
    assert code == 1 and message == "error : no sat ant pcv in igs.atx"
    # a PPG_RNX2RTKP that is not there, or not a program
    monkeypatch.setenv("PPG_RNX2RTKP", str(tmp_path / "nowhere" / "rnx2rtkp"))
    with pytest.raises(RuntimeError, match="PPG_RNX2RTKP"):
        _native.ppk_solve(str(conf), files, str(out))
    if os.name != "nt":  # Windows has no execute permission
        text_file = tmp_path / "rnx2rtkp.txt"
        text_file.write_text("not a program")
        monkeypatch.setenv("PPG_RNX2RTKP", str(text_file))
        with pytest.raises(RuntimeError, match="PPG_RNX2RTKP"):
            _native.ppk_solve(str(conf), files, str(out))


@pytest.mark.parametrize("name", ["base.25d", "BASE.25D", "rover.obs.gz", "rover.25o.Z", "base.crx", "obs.zip",
                                  "obs.tar", "obs.TAR.GZ"])
def test_ppk_refuses_compressed_inputs(name, tmp_path, monkeypatch):
    """RTKLIB decompresses gzip, zip, tar and Hatanaka files by their extension through a shell
    command (a fork of the program, the output beside the user's file): refused before RTKLIB or
    rnx2rtkp run, with or without the core."""
    conf, out = tmp_path / "ppk.conf", tmp_path / "rover.pos"
    conf.write_text("pos1-posmode =kinematic\n")
    files = [str(tmp_path / "rover.25o"), str(tmp_path / name), str(tmp_path / "base.25n")]
    assert _native.compressed_name(name) and not any(_native.compressed_name(f) for f in files[::2])
    assert not any(_native.compressed_name(n) for n in ("x.obs", "x.rnx", "x.nav", "x.25p", "x.sp3", "x.rtcm3", "x"))
    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.setenv("PPG_RNX2RTKP", str(_fake_rnx2rtkp(tmp_path)))
    with pytest.raises(ValueError, match="decompress") as error:
        _native.ppk_solve(str(conf), files, str(out))
    assert name in str(error.value)
    assert not (tmp_path / "run.log").exists() and not out.exists()  # rnx2rtkp never ran
    # with the core too, before it is called
    monkeypatch.setenv("PPG_NATIVE", "1")

    def core_ppk(*args):
        raise AssertionError("the core ran with a compressed input")

    monkeypatch.setattr(_native, "kernel", lambda fn: core_ppk if fn == "ppk_solve" else None)
    with pytest.raises(ValueError, match="decompress"):
        _native.ppk_solve(str(conf), files, str(out))
