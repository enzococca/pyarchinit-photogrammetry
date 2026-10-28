"""The GNSS pictures of the desktop guide (``docs/gui/photos-markers.md``): a flight of a drone over the demo trench,
made up - its ``Timestamp.MRK``, the drone's and a base's RINEX observations, navigation files - and the photos of the
demo given the PPK positions of that flight against the nearest permanent station, in memory only (the demo
project is never saved). Nothing here is a real survey: the photos are the rendered ones of the demo, the observations
are numbers in the RINEX format, the solution is made from the cameras of the demo; only the station and its position
are real, those of the public lists of the EPN, the IGS and RING.

Used by ``tools/docs_screenshots.py``, whose worker imports this file by path."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

GPS_EPOCH = datetime(1980, 1, 6)
LEAP_SECONDS = 18  # GPS time less UTC in 2026
FIRST_SHOT = datetime(2026, 10, 6, 9, 12, 18, 200000)  # GPS time (a Tuesday morning: 11:12 in Italy)
STEP_S = 15.0  # between two shots
ROVER = (datetime(2026, 10, 6, 9, 10, 0), datetime(2026, 10, 6, 9, 21, 0))  # the drone's observations, GPS time
BASE = (datetime(2026, 10, 6, 8, 40, 0), datetime(2026, 10, 6, 11, 10, 0))  # the user's own base: 2.5 hours
NAME = "DJI_202610061112_001"  # as a DJI drone names the files of a flight (its local time)
LEVER_ARM_MM = (21.0, -5.0, 189.0)  # the camera from the antenna: north, east, down
FLOAT, SINGLE = (8, 9), (20,)  # the shots (counted from 0) the solution gives as float and as single
SATELLITES = ["G02", "G05", "G07", "G13", "G14", "G18", "G20", "G30", "E04", "E09", "E11", "E36"]


@dataclass
class Scene:
    folder: Path  # the files of the flight: drone/ (its card) and base/ (the base receiver)
    mrk: Path
    rover: Path
    base: Path  # the observations of the user's own base (for the PPP)
    station: object  # stations.Station: the base of the PPK
    distance_km: float
    lat: float  # the survey (degrees)
    lon: float
    when: datetime  # the flight (UTC)
    result: dict  # what apply_ppk said


def _line(text: str, label: str) -> str:
    return f"{text:<60}{label}"


def _week(when: datetime) -> tuple[int, float]:
    """The GPS week and the seconds of the week of ``when`` (GPS time)."""
    seconds = (when - GPS_EPOCH).total_seconds()
    week = int(seconds // 604800)
    return week, seconds - 604800 * week


def _seconds(when: datetime) -> float:
    return (when - GPS_EPOCH).total_seconds()


def _first_obs(when: datetime) -> str:
    return (f"{when.year:6d}{when.month:6d}{when.day:6d}{when.hour:6d}{when.minute:6d}"
            f"{when.second + when.microsecond / 1e6:13.7f}     GPS")


def _value(i: int, j: int, k: int) -> float:
    """A number that looks like an observation: pseudorange, phase, Doppler or signal strength."""
    return (21_000_000.0 + 150_000.0 * i + 37.0 * k, 110_000_000.0 + 790_000.0 * i + 194.0 * k,
            -1200.0 + 210.0 * i, 42.0 + (i % 5))[j]


def _observations3(path: Path, marker: str, receiver: str, antenna: str, xyz, start: datetime, end: datetime,
                   step: float) -> Path:
    head = [_line("     3.04           OBSERVATION DATA    M: Mixed", "RINEX VERSION / TYPE"),
            _line(f"{'pyarchinit docs':<20}{'':<20}{start:%Y%m%d %H%M%S} GPS", "PGM / RUN BY / DATE"),
            _line(marker, "MARKER NAME"),
            _line(f"{'':<20}{receiver:<20}", "REC # / TYPE / VERS"),
            _line(f"{'':<20}{antenna:<20}", "ANT # / TYPE"),
            _line("".join(f"{v:14.4f}" for v in xyz), "APPROX POSITION XYZ"),
            _line("".join(f"{v:14.4f}" for v in (0.0, 0.0, 0.0)), "ANTENNA: DELTA H/E/N"),
            _line("G    4 C1C L1C D1C S1C", "SYS / # / OBS TYPES"),
            _line("E    4 C1X L1X D1X S1X", "SYS / # / OBS TYPES"),
            _line(f"{step:10.3f}", "INTERVAL"),
            _line(_first_obs(start), "TIME OF FIRST OBS"),
            _line(_first_obs(end), "TIME OF LAST OBS"),
            _line("", "END OF HEADER")]
    lines = list(head)
    for k in range(int(round((end - start).total_seconds() / step)) + 1):
        t = start + timedelta(seconds=k * step)
        lines.append(f"> {t:%Y %m %d %H %M}{t.second:11.7f}  0{len(SATELLITES):3d}")
        lines += [sat + "".join(f"{_value(i, j, k):14.3f}  " for j in range(4)) for i, sat in enumerate(SATELLITES)]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _observations2(path: Path, marker: str, receiver: str, antenna: str, xyz, delta_h: float, start: datetime,
                   end: datetime, step: float) -> Path:
    head = [_line("     2.11           OBSERVATION DATA    M (MIXED)", "RINEX VERSION / TYPE"),
            _line(f"{'pyarchinit docs':<20}{'':<20}{start:%Y%m%d %H%M%S} GPS", "PGM / RUN BY / DATE"),
            _line(marker, "MARKER NAME"),
            _line(f"{'5432K10987':<20}{receiver:<20}{'6.13':<20}", "REC # / TYPE / VERS"),
            _line(f"{'1440912345':<20}{antenna:<20}", "ANT # / TYPE"),
            _line("".join(f"{v:14.4f}" for v in xyz), "APPROX POSITION XYZ"),
            _line("".join(f"{v:14.4f}" for v in (delta_h, 0.0, 0.0)), "ANTENNA: DELTA H/E/N"),
            _line("     4    C1    L1    D1    S1", "# / TYPES OF OBSERV"),
            _line(f"{step:10.3f}", "INTERVAL"),
            _line(_first_obs(start), "TIME OF FIRST OBS"),
            _line(_first_obs(end), "TIME OF LAST OBS"),
            _line("", "END OF HEADER")]
    sats = "".join(s[0] + f"{int(s[1:]):2d}" for s in SATELLITES)
    lines = list(head)
    for k in range(int(round((end - start).total_seconds() / step)) + 1):
        t = start + timedelta(seconds=k * step)
        lines.append(f" {t.year % 100:02d}{t.month:3d}{t.day:3d}{t.hour:3d}{t.minute:3d}{t.second:11.7f}  0"
                     f"{len(SATELLITES):3d}{sats}")
        lines += ["".join(f"{_value(i, j, k):14.3f}  " for j in range(4)) for i in range(len(SATELLITES))]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _d(value: float) -> str:
    """A number of a navigation message (D19.12)."""
    return f"{value:19.12E}".replace("E", "D")


def _navigation2(path: Path, when: datetime) -> Path:
    head = [_line("     2.11           N: GPS NAV DATA", "RINEX VERSION / TYPE"),
            _line(f"{'pyarchinit docs':<20}{'':<20}{when:%Y%m%d %H%M%S} GPS", "PGM / RUN BY / DATE"),
            _line(f"{LEAP_SECONDS:6d}", "LEAP SECONDS"), _line("", "END OF HEADER")]
    record = [f"{5:2d} {when.year % 100:02d}{when.month:3d}{when.day:3d}{when.hour:3d}{when.minute:3d}"
              f"{float(when.second):5.1f}" + _d(1.2e-5) + _d(-1.1e-12) + _d(0.0)]
    record += ["   " + "".join(_d(v) for v in values) for values in
               [(23.0, -12.5, 4.6e-9, 1.2), (-6.3e-7, 5.1e-3, 8.9e-6, 5153.7), (208800.0, 1.1e-7, -2.1, 3.2e-8),
                (0.96, 220.1, 0.71, -8.1e-9), (1.9e-10, 1.0, 2335.0, 0.0), (2.0, 0.0, -1.1e-8, 23.0),
                (201618.0, 4.0)]]
    path.write_text("\n".join(head + record) + "\n", encoding="ascii")
    return path


def _navigation3(path: Path, when: datetime) -> Path:
    head = [_line("     3.04           N: GNSS NAV DATA    M: Mixed", "RINEX VERSION / TYPE"),
            _line(f"{'pyarchinit docs':<20}{'':<20}{when:%Y%m%d %H%M%S} GPS", "PGM / RUN BY / DATE"),
            _line(f"{LEAP_SECONDS:6d}", "LEAP SECONDS"), _line("", "END OF HEADER")]
    record = [f"G05 {when:%Y %m %d %H %M %S}" + _d(1.2e-5) + _d(-1.1e-12) + _d(0.0)]
    record += ["    " + "".join(_d(v) for v in values) for values in
               [(23.0, -12.5, 4.6e-9, 1.2), (-6.3e-7, 5.1e-3, 8.9e-6, 5153.7), (208800.0, 1.1e-7, -2.1, 3.2e-8),
                (0.96, 220.1, 0.71, -8.1e-9), (1.9e-10, 1.0, 2335.0, 0.0), (2.0, 0.0, -1.1e-8, 23.0),
                (201618.0, 4.0)]]
    path.write_text("\n".join(head + record) + "\n", encoding="ascii")
    return path


def make_scene(chunk, photos: Path, folder: Path) -> Scene:
    """The made-up flight over ``chunk`` (the demo's), its files written in ``folder``, the photos of ``photos`` (the
    demo's folder of photos, one shot each in the order of their names) given PPK positions against the user's own
    base placed by a static PPP, in memory."""
    from pyarchinit_photogrammetry.core import crs as crs_mod
    from pyarchinit_photogrammetry.gnss import apply, atx, dji, frames, ppk, rinex, stations

    drone, ground = folder / "drone", folder / "base"  # the card of the drone, the download of the base receiver
    drone.mkdir(parents=True, exist_ok=True)
    ground.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in photos.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".tif", ".png"))
    by_name = {Path(c.path).name: c for c in chunk.cameras}
    cams = [by_name[p.name] for p in files]
    # the time each camera wrote in its photo (local time, 2 hours ahead of UTC in October): the demo's have none
    for i, cam in enumerate(cams):
        local = FIRST_SHOT + timedelta(seconds=STEP_S * i - LEAP_SECONDS, hours=2)
        cam.meta["datetime"] = f"{local:%Y:%m:%d %H:%M:%S}"
    centres = chunk.to_world(np.array([c.transform[:3, 3] for c in cams], dtype=float))
    mid = FIRST_SHOT + timedelta(seconds=STEP_S * (len(cams) - 1) / 2)
    when = (mid - timedelta(seconds=LEAP_SECONDS)).replace(tzinfo=timezone.utc)
    epoch = frames.decimal_year(when)
    llh_cam = crs_mod.transform_points(centres, chunk.crs, frames.FRAMES["ITRF2020"], epoch=epoch)  # lon, lat, h
    rng = np.random.default_rng(6)
    shots, llh, q, sd = [], [], [], []
    for i, (lon, lat, h) in enumerate(llh_cam):
        n, e, v = LEVER_ARM_MM
        alon, alat, ah = dji.lever_arm(lat, lon, h, (-n, -e, -v))  # the antenna above the camera
        state = 2 if i in FLOAT else 5 if i in SINGLE else 1
        sigma = {1: (0.006, 0.006, 0.012), 2: (0.07, 0.06, 0.15), 5: (0.9, 0.8, 1.9)}[state]
        noise = rng.normal(0.0, 0.5, 3) * np.array(sigma)
        metres = 111_320.0
        llh.append((alat + noise[1] / metres, alon + noise[0] / (metres * np.cos(np.radians(alat))), ah + noise[2]))
        q.append(state)
        sd.append(sigma)
        t = FIRST_SHOT + timedelta(seconds=STEP_S * i + float(rng.uniform(0, 0.4)))
        shots.append(t)
    # the Timestamp.MRK: the drone's own positions are single (no RTK link), a metre or two off
    lines = []
    for i, (t, (alat, alon, ah)) in enumerate(zip(shots, llh)):
        week, sow = _week(t)
        off = rng.normal(0.0, 1.0, 3) * np.array((1.2, 1.0, 2.2))
        lines.append(f"{i + 1}\t{sow:.6f}\t[{week}]\t{LEVER_ARM_MM[0]:8.0f},N\t{LEVER_ARM_MM[1]:8.0f},E\t"
                     f"{LEVER_ARM_MM[2]:8.0f},V\t{alat + off[1] / 111_320.0:.8f},Lat\t"
                     f"{alon + off[0] / (111_320.0 * np.cos(np.radians(alat))):.8f},Lon\t{ah + off[2]:.3f},Ellh\t"
                     f"{1.4:.6f}, {1.2:.6f}, {2.6:.6f}\t16,Q")
    mrk = drone / f"{NAME}_Timestamp.MRK"
    mrk.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    first = llh[0]
    xyz = rinex.llh_to_ecef((first[0], first[1], first[2]))
    rover = _observations3(drone / f"{NAME}_Rinex.obs", "DJI", "DJI", "", xyz, ROVER[0], ROVER[1], 1.0)
    _navigation3(drone / f"{NAME}_PPKNAV.nav", ROVER[0])
    # the user's own base on a hill 180 m away, placed by a static PPP on the final products of ESA: the base of the
    # PPK (the permanent station nearby is only offered in the dialogs: nothing is made up about a real station)
    antenna = "TRM57971.00     NONE"
    lat, lon = float(llh_cam[:, 1].mean()), float(llh_cam[:, 0].mean())
    base_llh = (lat + 0.0011, lon - 0.0016, float(llh_cam[:, 2].mean()) + 6.0)  # ITRF2020 at the epoch of the flight
    guess = (base_llh[0] + 1.8e-5, base_llh[1] - 2.1e-5, base_llh[2] + 3.1)  # the receiver's own: metres off
    base = _observations2(ground / "BASE2790.26O", "BASE", "TRIMBLE R9S", antenna, rinex.llh_to_ecef(guess), 1.5530,
                          BASE[0], BASE[1], 1.0)
    _navigation2(ground / "BASE2790.26N", BASE[0])
    try:  # the calibration as RTKLIB finds it in igs20.atx (downloaded once into the GNSS folder of the program)
        table = atx.atx_file()
        entry, without = atx.lookup(table, antenna)
        calibration = {"file": table.name, "entry": entry, "without_radome": without,
                       "up_m": atx.up_offset(table, entry) if entry else None}
    except Exception:  # noqa: BLE001 - offline: as a run without a file of calibrations
        calibration = None
    day = f"{BASE[0]:%Y}{BASE[0].timetuple().tm_yday:03d}0000"
    base_record = {"name": "BASE", "position": [base_llh[1], base_llh[0], base_llh[2]], "position_source": "ppp",
                   "frame": "ITRF2020", "epoch": round(epoch, 4), "sd": [0.046, 0.041, 0.093], "antenna": antenna,
                   "antenna_delta_hen": [1.553, 0.0, 0.0], "file": base.name, "antenna_calibration": calibration,
                   "detail": {"centre": "ESA", "kind": "final", "hours": 2.5,
                              "products": [f"ESA0OPSFIN_{day}_01D_05M_ORB.SP3.gz",
                                           f"ESA0OPSFIN_{day}_01D_30S_CLK.CLK.gz",
                                           f"ESA0OPSFIN_{day}_01D_01D_ERP.ERP.gz"]}}
    # the nearest permanent station (of the EPN, the IGS or RING), for the dialogs
    station, distance = stations.nearby(lat, lon, limit=1)[0]
    solution = ppk.Solution(times=np.array([_seconds(t) for t in shots]), llh=np.array(llh), q=np.array(q),
                            ns=np.full(len(q), 17), sd=np.array(sd), base=base_record,
                            path=chunk_ppk_folder(chunk) / f"{NAME}_Rinex.pos", rover=rover.name,
                            inputs={"rover": str(rover), "base": [str(base)],
                                    "navs": [str(drone / f"{NAME}_PPKNAV.nav"), str(ground / "BASE2790.26N")]})
    result = apply.apply_ppk(chunk, mrk, solution, photos)
    return Scene(folder, mrk, rover, base, station, distance, lat, lon, when, result)


def chunk_ppk_folder(chunk) -> Path:
    from pyarchinit_photogrammetry.gnss import ppk

    return ppk.work_folder(chunk)
