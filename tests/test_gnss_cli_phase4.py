"""The command line of the GNSS work, phase 4: ppg gnss convert, events and log, and ppg gnss ppk with shots of any
source."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.cli import app
import gnss_ulog
import test_gnss_convert as convert_test
import test_gnss_ppk_events as events_test
from gnss_helpers import jpeg, needs_rtklib

PHASE4 = Path(__file__).parent / "data" / "gnss" / "phase4"
GPS_EPOCH = datetime(1980, 1, 6)
ZONE = timedelta(hours=2)
needs_a_converter = pytest.mark.skipif(not _native.convbin_available(),
                                       reason="no converter: native core older than 0.8 and no convbin")


def _run(*args: str):
    return CliRunner().invoke(app, [str(a) for a in args])


def _project(tmp_path: Path, gps_times: dict[int, float], leap: int, zone: timedelta = ZONE) -> tuple[Path, Path]:
    """A saved project with a photo per shot (``IMG_<number>.JPG``, the local time its camera wrote, to the second)."""
    folder = tmp_path / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    for number, t in gps_times.items():
        utc = GPS_EPOCH + timedelta(seconds=t - leap)
        jpeg(folder / f"IMG_{number:04d}.JPG", None, when=(utc + zone).replace(microsecond=0))
    doc = ppg.Document()
    doc.add_chunk("Volo").add_photos([folder])
    doc.save(tmp_path / "volo.ppg")
    return tmp_path / "volo.ppg", folder


@needs_a_converter
def test_cli_convert_ubx(tmp_path):
    out = tmp_path / "rinex"
    result = _run("gnss", "convert", PHASE4 / "ubx_20080526_64k.ubx", "--out", out)
    assert result.exit_code == 0, result.output
    assert "ubx_20080526_64k.ubx (ubx): " in result.output and " epochs, 0 events" in result.output
    assert (out / "ubx_20080526_64k.obs").is_file() and (out / "ubx_20080526_64k.nav").is_file()
    assert f"observations: {out / 'ubx_20080526_64k.obs'}" in result.output


@needs_a_converter
def test_cli_convert_dji_dat(tmp_path):
    dat = convert_test._dji_dat(tmp_path)
    refused = _run("gnss", "convert", dat)
    assert refused.exit_code == 1 and "approximate time" in refused.output
    result = _run("gnss", "convert", dat, "--approx-time", "2012-10-14")
    assert result.exit_code == 0, result.output
    assert "DJI_202210140000_001.DAT (rtcm3): " in result.output
    like = _run("gnss", "convert", dat, "--like", PHASE4 / "GMSD7_20121014_64k.obs")
    assert like.exit_code == 0, like.output


def test_cli_events_lists_times():
    result = _run("gnss", "events", PHASE4 / "ubx_20080526_64k_tm2.obs")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "ubx_20080526_64k_tm2.obs: 3 events (rinex_events), 3 with a valid time"
    rows = lines[2:5]
    assert [row.split()[1] for row in rows] == ["1481"] * 3
    assert [float(row.split()[2]) for row in rows] == pytest.approx([107975.5, 107990.25, 108005.123457], abs=1e-6)
    assert all(row.endswith("yes") for row in rows)


def _px4_log(tmp_path: Path, **options) -> tuple[Path, list[dict]]:
    w, expected = gnss_ulog.flight(12, interval_s=4.0, **options)
    path = tmp_path / "photos" / "flight.ulg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(w.binary())
    return path, expected


def test_cli_log_dry_run_changes_nothing(tmp_path):
    log, expected = _px4_log(tmp_path)
    project, folder = _project(tmp_path, {e["seq"]: e["true"] for e in expected}, gnss_ulog.LEAP_2025)
    before = project.read_bytes()
    result = _run("gnss", "log", project, log, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "flight.ulg: 12 shots (px4_capture), 12 with a valid time" in result.output
    assert "12 of 12 photos pair with a shot (by number)" in result.output
    assert "IMG_0003.JPG <- shot 3 at " in result.output
    assert result.output.rstrip().endswith("dry run: nothing changed")
    assert project.read_bytes() == before


def test_cli_log_applies_positions(tmp_path):
    """A PX4 log whose receiver gives no geoid: the photos take the AMSL position of their shot, marked msl, and
    ppg gnss status says so."""
    log, expected = _px4_log(tmp_path, separation=None, layout="1.7")
    project, folder = _project(tmp_path, {e["seq"]: e["true"] for e in expected}, gnss_ulog.LEAP_2025)
    result = _run("gnss", "log", project, log, "--lever", "0,0,250")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("flight.ulg: 12 shots")
    assert "12 of 12 photos took the position of their shot (fix 12)" in result.output
    assert "marked 'msl'" in result.output
    doc = ppg.Document.open(project)
    by = {e["seq"]: e for e in expected}
    for cam in doc.chunk.cameras:
        e = by[int(cam.label[-4:])]
        lon, lat, h = cam.reference.location
        assert (lat, lon) == pytest.approx((e["lat"], e["lon"]), abs=1e-6)
        assert h == pytest.approx(e["msl"] - 0.25, abs=1e-3)  # the camera 25 cm under the antenna, level drone
        assert cam.meta["gnss"]["height"] == "msl"
    status = _run("gnss", "status", project)
    assert status.exit_code == 0 and "msl:12" in status.output, status.output


def test_cli_log_options_checked(tmp_path):
    log, expected = _px4_log(tmp_path)
    project, _folder = _project(tmp_path, {e["seq"]: e["true"] for e in expected}, gnss_ulog.LEAP_2025)
    assert _run("gnss", "log", project, log, "--message", "cam").exit_code == 2
    assert _run("gnss", "log", project, log, "--lever", "1,2").exit_code == 2
    assert _run("gnss", "log", project, log, "--by", "colour").exit_code == 2


@needs_rtklib
def test_cli_ppk_with_rover_events(tmp_path, monkeypatch):
    """The rover's RINEX with three events: --shots events takes them from the rover, the photos the PPK position
    of their events with the lever arm in the body (no attitude: its vertical part only, said)."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    times = [t for _m, t in events_test.MARKS[:3]]
    project, folder = _project(tmp_path, {i: t for i, t in enumerate(times, 1)}, events_test.LEAP_2008,
                               zone=events_test.ZONE)
    base = events_test._alone(events_test.UBX_OBS, tmp_path / "base")
    result = _run("gnss", "ppk", project, "--rover", events_test.TM2_OBS, "--base", base, "--nav",
                  events_test.UBX_NAV, "--shots", "events", "--photos", folder, "--by", "order", "--lever", "0,0,250")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("3 of 3 photos took the PPK position of their shot")
    assert "vertical part" in result.output
    doc = ppg.Document.open(project)
    assert all(cam.meta["gnss"]["shots"]["kind"] == "rinex_events" for cam in doc.chunk.cameras)


def test_cli_ppk_mrk_options_unchanged(tmp_path):
    """--mrk is --shots mrk --shots-file: not both; the options of the other sources refused with an MRK; no shots
    at all said."""
    mrk = tmp_path / "Timestamp.MRK"
    mrk.write_text("")
    rover = PHASE4 / "ubx_20080526_64k.obs"
    project = tmp_path / "volo.ppg"
    ppg.Document().save(project)
    both = _run("gnss", "ppk", project, "--mrk", mrk, "--shots", "events", "--rover", rover, "--base", rover)
    assert both.exit_code == 1 and "--mrk is --shots mrk" in both.output
    lever = _run("gnss", "ppk", project, "--mrk", mrk, "--lever", "0,0,100", "--rover", rover, "--base", rover)
    assert lever.exit_code == 1 and "lever arm of each shot" in lever.output
    none = _run("gnss", "ppk", project, "--rover", rover, "--base", rover)
    assert none.exit_code == 1 and "give the shots" in none.output
