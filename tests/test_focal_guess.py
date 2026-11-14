"""Photos that do not say their focal length: where the focal length comes from, the guess, the
warning, and the alignment tried again from other focal lengths when the guess leaves photos out."""
from __future__ import annotations

import logging

import cv2
import numpy as np
import pytest
from PIL import Image

from pyarchinit_photogrammetry.core.camera import FOCAL_GUESS, Sensor
from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.io.exif import focal_px, read_exif


def _jpeg(path, exif_ifd: dict | None = None, size=(600, 400)):
    img = np.random.default_rng(0).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    im = Image.fromarray(img)
    if exif_ifd is None:
        im.save(path, quality=90)
        return path
    exif = Image.Exif()
    exif[0x8769] = exif_ifd
    im.save(path, quality=90, exif=exif.tobytes())
    return path


def test_focal_length_from_the_exif(tmp_path):
    assert focal_px({"width": 600, "height": 400, "focal_length_35mm": 24.0}) == (400.0, "exif")
    # 6 mm lens, 4 micrometre pixels (FocalPlaneXResolution 250 per mm): 1500 px
    info = read_exif(_jpeg(tmp_path / "a.jpg", {0x920A: 6.0, 0xA20E: 250.0, 0xA210: 4}))
    assert focal_px(info) == (pytest.approx(1500.0), "exif_mm")
    # the same camera, the photo halved afterwards (PixelXDimension says the size the resolution is for)
    info = read_exif(_jpeg(tmp_path / "b.jpg", {0x920A: 6.0, 0xA20E: 250.0, 0xA210: 4, 0xA002: 1200}))
    assert focal_px(info) == (pytest.approx(750.0), "exif_mm")
    assert focal_px({"width": 600, "height": 400}) == (None, None)
    assert focal_px({"width": 600, "height": 400, "focal_length_35mm": 1.0}) == (None, None)  # no such lens
    # a focal plane resolution that would make a 1 km sensor: not believed
    assert focal_px({"width": 600, "height": 400, "focal_length_mm": 6.0, "focal_plane_mm_per_px": 10.0}) == (None, None)


def test_photos_without_focal_length_get_a_guess_and_a_warning(tmp_path, caplog):
    folder = tmp_path / "photos"
    folder.mkdir()
    for i in range(2):
        _jpeg(folder / f"P{i}.jpg")
    ch = Document().add_chunk("c")
    with caplog.at_level(logging.WARNING):
        ch.add_photos([folder])
    (s,) = ch.sensors.values()
    assert s.focal_source == "guess" and s.focal_guessed
    assert s.focal_px == pytest.approx(FOCAL_GUESS * 600)
    assert "no focal length" in caplog.text
    assert Sensor.from_dict(s.to_dict()).focal_source == "guess"
    assert Sensor.from_dict({k: v for k, v in s.to_dict().items() if k != "focal_source"}).focal_source is None
    exif = tmp_path / "exif"
    exif.mkdir()
    _jpeg(exif / "E.jpg", {0xA405: 36})
    ch2 = Document().add_chunk("d")
    ch2.add_photos([exif])
    (s2,) = ch2.sensors.values()
    assert s2.focal_source == "exif" and not s2.focal_guessed and s2.focal_px == pytest.approx(600.0)


def test_the_alignment_is_tried_again_from_other_focal_lengths(tmp_path, monkeypatch):
    """The alignment from the guess places 3 photos of 30; from 0.75 times it 12, from 1.4 times
    it 29: the last one is kept, the database keeps the guess."""
    from pyarchinit_photogrammetry.engine import sfm

    ch = Document().add_chunk("c")
    s = Sensor(0, "camera", 1000, 750, params=[850.0, 850.0, 500.0, 375.0, 0, 0, 0, 0], focal_source="guess")
    ch.sensors[0] = s
    ch.meta["sfm"] = {"names": {str(i): f"P{i}.jpg" for i in range(30)}}
    db_focal, calls = {}, []
    monkeypatch.setattr(sfm, "_sfm_dir", lambda chunk: tmp_path)
    monkeypatch.setattr(sfm, "_set_db_focals", lambda db, chunk, focals: db_focal.update(focals))

    def fake_align(chunk, progress=None, **kw):
        f = db_focal[0]
        calls.append(f)
        chunk.sensors[0].params[0] = chunk.sensors[0].params[1] = 1100.0
        chunk.sensors[0].focal_source = "aligned"
        return {850.0: 3, 637.5: 12, 1190.0: 29}[round(f, 1)]

    monkeypatch.setattr(sfm, "_align_cameras", fake_align)
    assert sfm.align_cameras(ch) == 29
    assert calls == [850.0, 637.5, 1190.0]
    assert db_focal[0] == 850.0  # aligning again starts from the guess again
    assert ch.meta["sfm"]["focal_guess"] == {"tried": [{"factor": 1.0, "aligned": 3}, {"factor": 0.75, "aligned": 12},
                                                       {"factor": 1.4, "aligned": 29}], "kept": 1.4}
    # a better one in the middle: aligned again from it at the end
    calls.clear()
    ch.sensors[0].params[:2] = [850.0, 850.0]
    ch.sensors[0].focal_source = "guess"
    monkeypatch.setattr(sfm, "_align_cameras", lambda chunk, progress=None, **kw: (
        calls.append(db_focal[0]) or {850.0: 3, 637.5: 20, 1190.0: 5}[round(db_focal[0], 1)]))
    assert sfm.align_cameras(ch) == 20 and calls == [850.0, 637.5, 1190.0, 637.5]
    # a focal length that is not a guess: aligned once
    calls.clear()
    ch.sensors[0].focal_source = "exif"
    assert sfm.align_cameras(ch) == 3 and calls == [db_focal[0]] and "focal_guess" not in ch.meta["sfm"]


@pytest.mark.slow
def test_turntable_photos_without_focal_length_align(tmp_path):
    """The turntable demo without a focal length in the EXIF and with the start 1.6 times too long
    (a phone's 24 mm photos started from COLMAP's 1.2 times the width): alone it left half of the
    alignments at 2-3 photos; tried again from other focal lengths every photo is placed and the
    focal length found is the true one (1100 px)."""
    from pyarchinit_photogrammetry.testing.synthetic import write_pottery_dataset

    write_pottery_dataset(tmp_path / "t", exif_focal=False)
    ch = Document().add_chunk("c")
    ch.add_photos([tmp_path / "t" / "photos"])
    (s,) = ch.sensors.values()
    assert s.focal_guessed
    s.params[0] = s.params[1] = 1.6 * 1100.0
    ch.match_photos(quality="high")
    for _ in range(3):
        n = ch.align_cameras()
        assert n >= 30 and abs(ch.sensors[s.key].focal_px - 1100.0) < 10.0, (n, ch.meta["sfm"].get("focal_guess"))
        assert ch.sensors[s.key].focal_source == "aligned"
        ch.sensors[s.key].params[:] = [1760.0, 1760.0, 600.0, 450.0, 0.0, 0.0, 0.0, 0.0]
        ch.sensors[s.key].model, ch.sensors[s.key].focal_source = "OPENCV", "guess"
