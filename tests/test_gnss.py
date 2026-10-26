"""Precise camera positions: the RTK status of DJI photos and the accuracy it gives."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

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
