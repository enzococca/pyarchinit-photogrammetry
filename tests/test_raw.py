"""Camera raw files as photos (io.raw): developed once into the chunk folder, the camera on the developed copy.

The raws of the tests are written by ``testing.rawfile.write_dng`` (a Bayer DNG made from an image): no camera
file is needed and what the developed photo must look like is known."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.io import raw as rawio
from pyarchinit_photogrammetry.io.exif import read_exif
from pyarchinit_photogrammetry.testing.rawfile import write_dng

pytest.importorskip("rawpy")

GPS = (12.4924, 41.8902, 30.0)


def picture(seed: int = 0, size=(160, 120)) -> np.ndarray:
    """Smooth coloured blocks (RGB): what a demosaicked photo gives back within a few grey levels."""
    rng = np.random.default_rng(seed)
    w, h = size
    small = rng.integers(40, 200, (h // 8, w // 8, 3)).astype(np.uint8)
    return cv2.GaussianBlur(cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST), (0, 0), 3)


def raws(folder: Path, n: int = 2, **tags) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    tags = {"make": "PPG", "model": "Synthetic", "focal_length_mm": 24.0, "focal_length_35mm": 35.0, "gps": GPS,
            **tags}
    return [write_dng(folder / f"IMG_{i:04d}.dng", picture(i), **tags) for i in range(n)]


def project(tmp_path: Path):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    doc.save(tmp_path / "scavo.ppg")
    return doc, ch


def test_a_raw_develops_to_the_image_it_was_written_from(tmp_path):
    img = picture()
    src = write_dng(tmp_path / "a.dng", img)
    assert rawio.develop_file(src, tmp_path / "a.tif", {"format": "tif", "exposure": 0.0, "tone": "linear"}) == (160, 120)
    out = cv2.cvtColor(cv2.imread(str(tmp_path / "a.tif")), cv2.COLOR_BGR2RGB)
    assert out.shape == img.shape
    assert np.abs(out.astype(float) - img).mean() < 4.0  # grey levels: demosaicking, no shift, no flip
    assert not list(tmp_path.glob("*.part"))
    brighter = tmp_path / "b.jpg"
    rawio.develop_file(src, brighter, {"tone": "linear"})  # one stop more, as the JPEG of a camera
    assert cv2.imread(str(brighter)).mean() > out.mean() * 1.2
    camera = tmp_path / "c.tif"  # the default: with the contrast of a camera JPEG too, the pixels where they were
    rawio.develop_file(src, camera, {"format": "tif", "exposure": 0.0})
    toned = cv2.cvtColor(cv2.imread(str(camera)), cv2.COLOR_BGR2RGB).astype(float)
    assert toned.std() > out.std() * 1.2
    assert np.corrcoef(toned.mean(axis=2).ravel(), out.mean(axis=2).ravel())[0, 1] > 0.99
    table = rawio._tone_table()
    assert table[0] == 0 and table[-1] == 255 and (np.diff(table.astype(int)) >= 0).all()


def test_the_tone_follows_the_kind_of_raw_and_moves_nothing(tmp_path):
    """A linear development is flat: the raw of a camera (a mosaic) gets the contrast curve of a camera JPEG, the
    linear DNG of a phone the local contrast too, which the phone would add from its tone map. The alignment of
    real ProRAW frames depends on it (design/raw-photos.md); here: which tone "auto" is, and no pixel moved."""
    img = picture(3)

    def developed(src, tone):
        out = tmp_path / f"{src.stem}_{tone}.tif"
        rawio.develop_file(src, out, {"format": "tif", "exposure": 0.0, "tone": tone})
        return cv2.imread(str(out), cv2.IMREAD_GRAYSCALE).astype(np.float32)

    camera = write_dng(tmp_path / "camera.dng", img)
    phone = write_dng(tmp_path / "phone.dng", img, mosaic=False)
    assert np.abs(developed(phone, "linear") - cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)).mean() < 1.0
    for src, same, other in ((camera, "camera", "local"), (phone, "local", "camera")):
        auto = developed(src, "auto")
        assert np.array_equal(auto, developed(src, same)) and not np.array_equal(auto, developed(src, other))
        flat = developed(src, "linear")
        assert flat.std() * 1.2 < developed(src, "camera").std() < developed(src, "local").std()
        for tone in ("camera", "local"):
            (dx, dy), _response = cv2.phaseCorrelate(flat, developed(src, tone))  # half a pixel would be a bug
            assert abs(dx) < 0.2 and abs(dy) < 0.2, (src.name, tone, dx, dy)


def test_a_phone_raw_is_developed_with_its_own_tone_map(tmp_path):
    """Apple ProRAW keeps the phone's look in the file: BaselineExposure, a local tone map (ProfileGainTableMap)
    and a ProfileToneCurve, none of which LibRaw applies. The tone "file" applies them as the DNG specification
    says; "auto" takes it for a linear DNG that has the map. Here the map brightens the left of the frame twice as
    much as the right, the baseline exposure is half a stop, and the curve is the identity."""
    from pyarchinit_photogrammetry.testing.rawfile import linear

    img = picture(4)
    gains = np.empty((2, 2, 2), np.float32)
    gains[:, 0], gains[:, 1] = 2.0, 1.0  # left column of nodes, right column; the same for every input value
    look = {"baseline_exposure": 0.5, "tone_curve": [(0.0, 0.0), (1.0, 1.0)],
            "gain_table": {"gains": gains, "spacing": (1.0, 1.0), "origin": (0.0, 0.0)}}
    phone = write_dng(tmp_path / "phone.dng", img, mosaic=False, **look)

    def developed(src, **opts):
        out = tmp_path / f"{src.stem}_{len(list(tmp_path.glob('*.tif')))}.tif"
        rawio.develop_file(src, out, {"format": "tif", **opts})
        return cv2.cvtColor(cv2.imread(str(out)), cv2.COLOR_BGR2RGB).astype(np.float32)

    h, w = img.shape[:2]
    gain = 2.0 - (np.arange(w) + 0.5) / w  # from 2 at the left edge to 1 at the right one
    lin = np.clip(linear(img) * 2 ** 0.5 * gain[None, :, None], 0, 1)
    expected = 255 * np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    out = developed(phone, tone="file", detail=0)
    assert np.abs(out - expected).mean() < 3.0
    assert np.array_equal(developed(phone), developed(phone, tone="file"))  # "auto" is the file's own tone
    flat = write_dng(tmp_path / "flat.dng", img, mosaic=False)  # a linear DNG without a map: the local contrast
    assert np.array_equal(developed(flat), developed(flat, tone="local"))
    assert np.array_equal(developed(flat, tone="file"), developed(flat, tone="local"))
    camera = write_dng(tmp_path / "camera.dng", img, **look)  # a mosaic: the curve of a camera JPEG as before
    assert np.array_equal(developed(camera), developed(camera, tone="camera"))
    with pytest.raises(ValueError):
        rawio.settings({"detail": 9})


def test_the_detail_of_a_phone_raw_is_brought_out_as_the_phone_does(tmp_path):
    """The phone sharpens its JPEG at several scales; measured on real ProRAW frames the JPEG has two to three
    times the amplitude of the development at 0.05-0.3 cycles per pixel, and the frames match and align as the
    JPEGs only with that detail back. "auto" detail does it for the tone "file", 0 not; nothing moves."""
    img = picture(6, (320, 240))
    gains = np.ones((2, 2, 2), np.float32)
    phone = write_dng(tmp_path / "phone.dng", img, mosaic=False, baseline_exposure=0.0,
                      gain_table={"gains": gains}, tone_curve=[(0.0, 0.0), (1.0, 1.0)])

    def developed(**opts):
        out = tmp_path / f"d{len(list(tmp_path.glob('*.tif')))}.tif"
        rawio.develop_file(phone, out, {"format": "tif", "tone": "file", **opts})
        return cv2.imread(str(out), cv2.IMREAD_GRAYSCALE).astype(np.float32)

    plain, auto = developed(detail=0), developed()

    def band(g):
        return (cv2.GaussianBlur(g, (0, 0), 1.0) - cv2.GaussianBlur(g, (0, 0), 2.5)).std()

    assert band(auto) > 2.0 * band(plain)
    assert abs(auto.mean() - plain.mean()) < 2.0
    (dx, dy), _response = cv2.phaseCorrelate(plain, auto)
    assert abs(dx) < 0.1 and abs(dy) < 0.1
    assert np.array_equal(developed(detail=0, half_size=True).shape, (120, 160))


def test_the_libraries_of_the_pipeline_do_not_read_a_raw_but_read_its_copy(tmp_path):
    import pycolmap
    from PIL import Image

    src = write_dng(tmp_path / "a.dng", picture())
    assert cv2.imread(str(src)) is None  # why a raw is developed
    copy = tmp_path / "a.jpg"
    rawio.develop_file(src, copy)
    with Image.open(copy) as im:
        assert im.size == (160, 120)
    assert cv2.imread(str(copy)).shape == (120, 160, 3)
    bitmap = pycolmap.Bitmap.read(str(copy), True)
    assert (bitmap.width, bitmap.height) == (160, 120)


def test_exif_of_a_raw(tmp_path):
    src = write_dng(tmp_path / "a.dng", picture(), make="Canon", model="EOS Test", lens="EF 24mm",
                    focal_length_mm=24.0, focal_length_35mm=38.0, datetime="2026:05:04 10:20:30", gps=GPS,
                    orientation=6)
    info = read_exif(src)
    assert (info["width"], info["height"]) == (160, 120)  # the sensor, not a preview
    assert info["make"] == "Canon" and info["model"] == "EOS Test" and info["lens"] == "EF 24mm"
    assert info["focal_length_mm"] == 24.0 and info["focal_length_35mm"] == 38.0
    assert info["datetime"] == "2026:05:04 10:20:30" and info["orientation"] == 6
    assert np.allclose(info["gps"], GPS, atol=1e-6)


def test_raws_are_added_as_photos(tmp_path):
    doc, ch = project(tmp_path)
    files = raws(tmp_path / "foto")
    before = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
    cams = ch.add_photos([tmp_path / "foto"])
    assert [c.label for c in cams] == ["IMG_0000", "IMG_0001"]
    assert [(f.read_bytes(), f.stat().st_mtime_ns) for f in files] == before  # the raws are only read
    assert sorted(p.name for p in (tmp_path / "foto").iterdir()) == ["IMG_0000.dng", "IMG_0001.dng"]
    (sensor,) = ch.sensors.values()
    assert (sensor.width, sensor.height) == (160, 120)
    for cam, src in zip(cams, files):
        copy = Path(cam.path)
        assert copy.parent == ch.data_dir / "raw" and copy.suffix == ".jpg"
        assert cv2.imread(str(copy)).shape == (120, 160, 3)
        record = cam.meta["raw"]
        assert Path(record["path"]) == src.resolve() and record["size"] == src.stat().st_size
        assert record["settings"] == rawio.DEFAULTS
        assert cam.meta["make"] == "PPG" and cam.meta["focal_length_35mm"] == 35.0
        assert np.allclose(cam.reference.location, GPS, atol=1e-6)
        assert np.allclose(read_exif(copy)["gps"], GPS, atol=1e-6)  # the copy carries the EXIF of the raw
    doc.save()
    stored = json.loads((tmp_path / "scavo.ppg").read_text())["chunks"][0]["cameras"][0]
    assert stored["path"] == "./scavo.ppg.files/chunk_0/raw/IMG_0000.jpg"
    assert stored["meta"]["raw"]["path"] == "./foto/IMG_0000.dng"  # the project can be moved with its photos
    again = ppg.Document.open(tmp_path / "scavo.ppg").chunks[0]
    assert Path(again.cameras[0].meta["raw"]["path"]) == files[0].resolve()
    assert Path(again.cameras[0].path).is_file()


def test_frames_shot_upright_share_the_sensor_of_the_others(tmp_path):
    """The pixels stay as the sensor read them: an upright frame is not turned (one sensor model for all), its
    orientation is kept in the camera and not written into the copy (a reader would turn it)."""
    from PIL import Image

    doc, ch = project(tmp_path)
    (tmp_path / "foto").mkdir()
    write_dng(tmp_path / "foto" / "a.dng", picture(1))
    write_dng(tmp_path / "foto" / "b.dng", picture(2), orientation=6)
    a, b = ch.add_photos([tmp_path / "foto"])
    assert len(ch.sensors) == 1 and a.sensor_key == b.sensor_key
    assert b.meta["orientation"] == 6
    with Image.open(b.path) as im:
        assert im.size == (160, 120) and im.getexif().get(0x0112) in (None, 1)


def test_a_raw_and_a_jpeg_of_the_same_shot_are_one_photo(tmp_path):
    folder = tmp_path / "foto"
    raws(folder, 2)
    cv2.imwrite(str(folder / "IMG_0000.JPG"), picture(0))
    files, left = rawio.photo_files([folder], {".jpg"})
    assert [f.name for f in files] == ["IMG_0000.JPG", "IMG_0001.dng"] and [f.name for f in left] == ["IMG_0000.dng"]
    files, left = rawio.photo_files([folder], {".jpg"}, prefer="raw")
    assert [f.name for f in files] == ["IMG_0000.dng", "IMG_0001.dng"] and [f.name for f in left] == ["IMG_0000.JPG"]
    with pytest.raises(ValueError):
        rawio.photo_files([folder], {".jpg"}, prefer="both")

    doc, ch = project(tmp_path)
    said = []
    cams = ch.add_photos([folder], progress=lambda f, m="": said.append(m))
    assert sorted(Path(c.path).name for c in cams) == ["IMG_0000.JPG", "IMG_0001.jpg"]
    assert "raw" not in cams[0].meta and any("1 file non aggiunti" in m for m in said)
    assert ch.add_photos([folder]) == [] and ch.add_photos([folder], prefer="raw") == []  # the shots are there
    assert len(ch.cameras) == 2

    doc2, ch2 = project(tmp_path / "due")
    cams = ch2.add_photos([folder], prefer="raw")
    assert all(c.meta.get("raw") for c in cams) and len(cams) == 2
    assert ch2.add_photos([folder / "IMG_0000.JPG"]) == []  # nor the JPEG of a raw that is a photo


def test_two_raws_with_one_name_keep_their_copies(tmp_path):
    doc, ch = project(tmp_path)
    for name, seed in (("a", 1), ("b", 2)):
        (tmp_path / name).mkdir()
        write_dng(tmp_path / name / "IMG_1.dng", picture(seed))
    one, two = ch.add_photos([tmp_path / "a", tmp_path / "b"])
    assert Path(one.path).name == "IMG_1.jpg" and Path(two.path).name == "b_IMG_1.jpg"
    assert abs(float(cv2.imread(one.path).mean()) - float(cv2.imread(two.path).mean())) > 0.5


def test_copies_are_made_once_and_again_when_the_raw_or_the_settings_change(tmp_path):
    doc, ch = project(tmp_path)
    files = raws(tmp_path / "foto")
    cams = ch.add_photos(files)
    made = [Path(c.path).stat().st_mtime_ns for c in cams]
    assert ch.develop_raw_photos() == {"developed": 0, "missing": []}
    done = rawio.develop(ch.data_dir / "raw", files)  # the same files, the same settings: the copies are kept
    assert [d["path"].stat().st_mtime_ns for d in done.values()] == made

    write_dng(files[0], picture(7))  # the raw was edited
    os.utime(files[0], ns=(files[0].stat().st_atime_ns, files[0].stat().st_mtime_ns + 2_000_000_000))
    assert ch.develop_raw_photos()["developed"] == 1
    assert cams[0].meta["raw"]["mtime"] == rawio.stamp(files[0])["mtime"]

    mean = cv2.imread(cams[1].path).mean()
    assert ch.develop_raw_photos({"exposure": 0.0})["developed"] == 2
    assert cv2.imread(cams[1].path).mean() < mean and cams[1].meta["raw"]["settings"]["exposure"] == 0.0
    assert ch.develop_raw_photos({"format": "tif"})["developed"] == 2
    assert sorted(p.name for p in (ch.data_dir / "raw").iterdir()) == ["IMG_0000.tif", "IMG_0001.tif", "index.json"]
    assert cams[0].meta["raw"]["settings"] == {**rawio.DEFAULTS, "exposure": 0.0, "format": "tif"}

    Path(cams[0].path).unlink()  # a copy lost: made again
    files[1].unlink()  # a raw lost: said, its copy stays
    assert ch.develop_raw_photos() == {"developed": 1, "missing": ["IMG_0001"]}
    assert Path(cams[0].path).is_file() and Path(cams[1].path).is_file()
    for wrong in ({"format": "png"}, {"tone": "vivid"}, {"exposure": 9}, {"sharpen": 1}):
        with pytest.raises(ValueError):
            ch.develop_raw_photos(wrong)


def test_half_size_stays_as_the_photos_were_added(tmp_path):
    doc, ch = project(tmp_path)
    cams = ch.add_photos(raws(tmp_path / "foto"), raw={"half_size": True})
    (sensor,) = ch.sensors.values()
    assert (sensor.width, sensor.height) == (80, 60)
    assert ch.develop_raw_photos({"exposure": 0.5, "half_size": False})["developed"] == 2
    assert cv2.imread(cams[0].path).shape == (60, 80, 3) and cams[0].meta["raw"]["settings"]["half_size"] is True


def test_half_size_halves_every_raw(tmp_path):
    """LibRaw's half size takes one pixel per 2x2 cell of a sensor mosaic; a linear DNG (Apple ProRAW) has no
    mosaic and LibRaw leaves it whole. The copy is halved all the same, each pixel the mean of its 2x2 cell (the
    pixel centres where a mosaic's half size puts them: no shift)."""
    img = picture(5)
    for mosaic in (True, False):
        src = write_dng(tmp_path / f"m{int(mosaic)}.dng", img, mosaic=mosaic)
        full, half = tmp_path / f"m{int(mosaic)}_full.tif", tmp_path / f"m{int(mosaic)}_half.tif"
        opts = {"format": "tif", "exposure": 0.0, "tone": "linear"}
        assert rawio.develop_file(src, full, opts) == (160, 120)
        assert rawio.develop_file(src, half, {**opts, "half_size": True}) == (80, 60), mosaic
        big = cv2.imread(str(full), cv2.IMREAD_GRAYSCALE).astype(np.float32)
        small = cv2.imread(str(half), cv2.IMREAD_GRAYSCALE).astype(np.float32)
        assert small.shape == (60, 80)
        expected = cv2.resize(big, (80, 60), interpolation=cv2.INTER_AREA)
        assert np.abs(small - expected).mean() < 4.0, mosaic
        (dx, dy), _response = cv2.phaseCorrelate(expected, small)
        assert abs(dx) < 0.2 and abs(dy) < 0.2, (mosaic, dx, dy)


def test_cancelling_the_development(tmp_path):
    files = raws(tmp_path / "foto", 6)
    progress = Progress(lambda f, m: progress.cancel() if f > 0 else None)
    with pytest.raises(Cancelled):
        rawio.develop(tmp_path / "out", files, progress=progress, workers=1)
    assert not list((tmp_path / "out").glob("*.part"))
    kept = len(list((tmp_path / "out").glob("*.jpg")))
    assert 1 <= kept < 6
    done = rawio.develop(tmp_path / "out", files)  # the copies finished are found again
    assert len(done) == 6 and len(list((tmp_path / "out").glob("*.jpg"))) == 6


def test_without_rawpy_raws_are_refused_with_a_message(tmp_path, monkeypatch):
    doc, ch = project(tmp_path)
    folder = tmp_path / "foto"
    raws(folder, 1)
    cv2.imwrite(str(folder / "altra.jpg"), picture(3))
    monkeypatch.setattr(rawio, "available", lambda: False)
    with pytest.raises(rawio.RawUnavailable, match="rawpy"):
        ch.add_photos([folder])
    assert ch.cameras == []  # nothing half added
    assert len(ch.add_photos([folder / "altra.jpg"])) == 1  # the images need nothing


def test_a_file_that_is_no_raw_is_named(tmp_path):
    doc, ch = project(tmp_path)
    bad = tmp_path / "rotto.nef"
    bad.write_bytes(b"II*\x00" + b"\x00" * 200)
    with pytest.raises(ValueError, match="rotto.nef"):
        ch.add_photos([bad])
    assert ch.cameras == []


def test_save_as_reads_the_copies_of_the_new_folder(tmp_path):
    doc, ch = project(tmp_path)
    ch.add_photos(raws(tmp_path / "foto"))
    doc.save()
    doc.save(tmp_path / "altrove" / "copia.ppg")
    new = tmp_path / "altrove" / "copia.ppg.files" / "chunk_0" / "raw"
    assert [Path(c.path) for c in ch.cameras] == [new / "IMG_0000.jpg", new / "IMG_0001.jpg"]
    import shutil

    shutil.rmtree(tmp_path / "scavo.ppg.files")  # the first project is gone: the copy stands alone
    again = ppg.Document.open(tmp_path / "altrove" / "copia.ppg").chunks[0]
    assert all(Path(c.path).is_file() for c in again.cameras)
    assert again.develop_raw_photos() == {"developed": 0, "missing": []}


def test_a_duplicated_chunk_has_its_own_copies(tmp_path):
    doc, ch = project(tmp_path)
    files = raws(tmp_path / "foto")
    ch.add_photos(files)
    dup = doc.duplicate_chunk(ch)
    for a, b in zip(ch.cameras, dup.cameras):
        assert Path(b.path).parent == dup.data_dir / "raw" and Path(b.path) != Path(a.path)
        assert Path(b.path).is_file() and b.meta["raw"]["path"] == a.meta["raw"]["path"]
    (tmp_path / "altre").mkdir()
    write_dng(tmp_path / "altre" / "IMG_0000.dng", picture(9))
    before = Path(dup.cameras[0].path).read_bytes()
    (new,) = dup.add_photos([tmp_path / "altre"])
    assert Path(new.path).name == "altre_IMG_0000.jpg"  # the copy of the first photo is not written over
    assert Path(dup.cameras[0].path).read_bytes() == before


def test_relinking_raws_that_were_moved(tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import relink_photos

    doc, ch = project(tmp_path)
    ch.add_photos(raws(tmp_path / "foto"))
    copies = [c.path for c in ch.cameras]
    (tmp_path / "foto").rename(tmp_path / "spostate")
    assert ch.develop_raw_photos()["missing"] == ["IMG_0000", "IMG_0001"]
    Path(copies[0]).unlink()
    r = relink_photos(ch, tmp_path / "spostate")
    assert r["relinked"] == 2 and not r["not_found"]
    assert [c.path for c in ch.cameras] == copies and Path(copies[0]).is_file()  # the copy lost is made again
    assert Path(ch.cameras[0].meta["raw"]["path"]) == (tmp_path / "spostate" / "IMG_0000.dng").resolve()
    assert ch.develop_raw_photos() == {"developed": 0, "missing": []}


def test_gnss_pairing_takes_one_file_per_shot(tmp_path):
    from pyarchinit_photogrammetry.gnss.shots import _one_per_shot

    doc, ch = project(tmp_path)
    folder = tmp_path / "foto"
    files = raws(folder, 2)
    cv2.imwrite(str(folder / "IMG_0000.JPG"), picture(0))
    cv2.imwrite(str(folder / "IMG_0001.JPG"), picture(1))
    ch.add_photos([files[0]])  # the raw of the first shot is the photo; the second shot is not in the chunk
    sources = ch.photo_sources()
    one = _one_per_shot(folder.iterdir(), sources)
    assert [f.name for f in one] == ["IMG_0000.dng", "IMG_0001.JPG"]
    assert sources[one[0].resolve()] is ch.cameras[0]


def test_command_line(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = project(tmp_path)
    folder = tmp_path / "foto"
    raws(folder)
    cv2.imwrite(str(folder / "IMG_0000.jpg"), picture(0))
    run = CliRunner()
    r = run.invoke(app, ["add-photos", str(tmp_path / "scavo.ppg"), str(folder), "--prefer-raw", "--raw-format", "tif"])
    assert r.exit_code == 0 and "added 2 photos" in r.output, r.output
    ch = ppg.Document.open(tmp_path / "scavo.ppg").chunks[0]
    assert sorted(Path(c.path).name for c in ch.cameras) == ["IMG_0000.tif", "IMG_0001.tif"]
    r = run.invoke(app, ["develop-raw", str(tmp_path / "scavo.ppg"), "--raw-format", "jpg", "--raw-exposure", "0",
                         "--raw-detail", "1.5"])
    assert r.exit_code == 0 and "developed 2 raw files" in r.output, r.output
    ch = ppg.Document.open(tmp_path / "scavo.ppg").chunks[0]
    assert sorted(Path(c.path).name for c in ch.cameras) == ["IMG_0000.jpg", "IMG_0001.jpg"]
    assert ch.cameras[0].meta["raw"]["settings"]["exposure"] == 0.0
    assert ch.cameras[0].meta["raw"]["settings"]["detail"] == 1.5


def test_doctor_names_the_raw_decoder():
    from pyarchinit_photogrammetry.doctor import run_checks

    (check,) = [c for c in run_checks().checks if "raw" in c.name.lower()]
    assert rawio.version() and check.detail == rawio.version()


def test_raw_development_keeps_jpeg_decoding(tmp_path):
    """LibRaw comes with a libjpeg of its own: developing a raw, with SpatiaLite (the libjpeg of the system) loaded
    before or after, must leave COLMAP, OpenCV and PIL reading JPEG - and LibRaw developing."""
    src = write_dng(tmp_path / "a.dng", picture())
    img = tmp_path / "a.jpg"
    cv2.imwrite(str(img), picture(1))
    code = textwrap.dedent(f"""
        import sqlite3, sys
        from pyarchinit_photogrammetry.io import raw
        from pyarchinit_photogrammetry.pyarchinit.database import _warm_up_proj, _spatialite_paths
        def spatialite():
            _warm_up_proj()
            con = sqlite3.connect(":memory:")
            con.enable_load_extension(True)
            for p in _spatialite_paths():
                try:
                    con.load_extension(p)
                    return True
                except Exception:
                    continue
            return False
        def check(tag):
            import cv2, pycolmap
            from PIL import Image
            size = raw.develop_file({str(src)!r}, {str(tmp_path)!r} + "/" + tag + ".jpg")
            ok = (size == (160, 120) and pycolmap.Bitmap.read({str(img)!r}, True) is not None
                  and pycolmap.Bitmap.read({str(tmp_path)!r} + "/" + tag + ".jpg", True) is not None
                  and cv2.imread({str(img)!r}) is not None and Image.open({str(img)!r}).convert("RGB").size == (160, 120))
            print(tag, "OK" if ok else "FAIL")
        check("first")
        print("SPATIALITE" if spatialite() else "NOSPATIALITE")
        check("second")
    """)
    # close_fds=False: posix_spawn instead of fork (see test_spatialite_before_pycolmap_keeps_jpeg_decoding)
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180, close_fds=False)
    assert run.returncode == 0, run.stderr[-2000:]
    assert "first OK" in run.stdout and "second OK" in run.stdout, run.stdout + run.stderr[-2000:]


@pytest.mark.slow
def test_trench_shot_in_raw_keeps_the_metric_accuracy(tmp_path):
    """The synthetic trench photographed in raw: aligned and georeferenced as from its JPEGs (control < 2 mm,
    check points < 3 mm), so the developed copies carry the geometry of the sensor: no shift, no rotation."""
    from pyarchinit_photogrammetry.testing.synthetic import camera_poses, make_scene, render

    scene = make_scene()
    size, focal = (1200, 900), 1000.0
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    (tmp_path / "foto").mkdir()
    for i, pose in enumerate(camera_poses(scene)):
        write_dng(tmp_path / "foto" / f"IMG_{i:04d}.dng", cv2.cvtColor(render(scene, pose, K, size), cv2.COLOR_BGR2RGB))
    with open(tmp_path / "gcp.csv", "w") as fh:
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.4f},{y:.4f},{z:.4f}\n")
    doc, ch = project(tmp_path)
    ch.add_photos([tmp_path / "foto"])  # as a user adds them: the exposure and the tone curve of the default
    assert len(ch.cameras) == 28 and len(ch.sensors) == 1
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high")
    ch.align_cameras()
    assert len(ch.aligned_cameras) == 28 and len(ch.markers) == 8
    ch.import_reference(tmp_path / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    assert report["control_rmse"] < 0.002
    (sensor,) = ch.sensors.values()
    fx, _fy, cx, cy, k1 = sensor.params[:5]
    assert abs(fx / 1000 - 1) < 0.005 and abs(k1 + 0.04) < 0.005  # the synthetic camera, as from the JPEGs
    assert abs(cx - 600) < 1.5 and abs(cy - 450) < 1.5
    for label in ("target 4", "target 5"):
        ch.marker(label).checkpoint = True
    assert ch.update_transform()["check_rmse"] < 0.003


def test_window_adds_raws(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch = project(tmp_path)
    files = raws(tmp_path / "foto")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    asked = []

    def chosen(*args, **kwargs):
        asked.append(args[3])
        return [str(f) for f in files], ""

    monkeypatch.setattr(mw.QFileDialog, "getOpenFileNames", staticmethod(chosen))
    w.add_photos()
    w.runner.wait()
    app.processEvents()
    assert "*.dng" in asked[0] and "*.CR2" in asked[0] and "*.nef" in asked[0]  # the raws can be chosen
    assert [c.label for c in w.chunk.cameras] == ["IMG_0000", "IMG_0001"]
    assert all(Path(c.path).parent == w.chunk.data_dir / "raw" for c in w.chunk.cameras)
    assert w.photos.count() == 2
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_window_adds_a_folder_of_raws_and_shows_them(tmp_path, monkeypatch):
    """*Aggiungi cartella di foto* on a folder with a raw, a raw + JPEG of one shot and a phone's linear DNG: one
    photo per shot, thumbnails of the copies, and the photo view opens a photo backed by a raw."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch = project(tmp_path)
    folder = tmp_path / "foto"
    raws(folder, 2)
    write_dng(folder / "IMG_0002.DNG", picture(2), mosaic=False)
    cv2.imwrite(str(folder / "IMG_0001.JPG"), cv2.cvtColor(picture(1), cv2.COLOR_RGB2BGR))
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder)))
    w.add_photos(folder=True)
    w.runner.wait()
    app.processEvents()
    cams = w.chunk.cameras
    assert [(c.label, bool(c.meta.get("raw"))) for c in cams] == [
        ("IMG_0000", True), ("IMG_0001", False), ("IMG_0002", True)]  # the JPEG of the pair is the photo
    assert w.photos.count() == 3
    raw_cam = cams[2]
    w.open_photo(raw_cam)
    app.processEvents()
    assert w.photo_view.camera is raw_cam
    assert w.photo_view.pixmap_item is not None and w.photo_view.pixmap_item.pixmap().width() == 160
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_a_damaged_dng_does_not_ask_for_gigabytes(tmp_path):
    """A SubIFD tag that claims four thousand million entries: the look is not there, nothing is allocated."""
    import struct

    from pyarchinit_photogrammetry.io import raw

    path = tmp_path / "damaged.dng"
    path.write_bytes(b"II*\x00" + struct.pack("<L", 8) + struct.pack("<H", 1)
                     + struct.pack("<HHLL", 330, 4, 0xFFFFFFF0, 26) + struct.pack("<L", 0) + b"\x00" * 64)
    import tracemalloc

    tracemalloc.start()
    try:
        assert raw._dng_look(path) is None
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 100e6, f"{peak / 1e9:.1f} GB asked for a file of 100 bytes"
