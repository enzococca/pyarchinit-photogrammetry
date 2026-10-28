"""The GNSS engine on a real day of a permanent station: the static PPP of the station, a static baseline to a station
of the EPN 20 to 60 km away and the Hatanaka files of the archive, checked against the official numbers. The workflow
gnss-check runs it on demand, with the native core built from the branch:

    python .github/scripts/gnss_station_check.py [--station MEDI00ITA] [--day 2026-10-06] [--partner BOLG00ITA]
        [--crx2rnx RNXCMP_4.2.0_Linux_gcc_64bit/CRX2RNX] [--work DIR]

The program downloads what it needs as it does for a user - the day of the two stations from BKG
(stations.download_observations), the precise products of ESA, CODE or the IGS (ppp.products), igs20.atx, the broadcast
navigation - one file at a time, into PPG_GNSS_DIR, which the script points to a folder of --work when it is not set:
never the program's own folder. The truth is the multi-year solution of the EPN in IGS20 (the SSC file of the EPN
Central Bureau and its catalogue of discontinuities, CC BY 4.0), read here and not by the program: the position the
program gives the station (stations.coordinates) is printed beside it. The official CRX2RNX is the one of RNXCMP 4.2.0
of GSI, which the workflow downloads.

The checks; every number is printed, and the script exits with 1 when one fails:

* the static PPP of the day (24 hours: ppp.solve_static on the products ppp.products finds) against the SSC coordinates
  of the station at the epoch: at most 0.03 m in plan and 0.06 m in height. Sessions of 4 and 2 hours cut from the same
  day are printed only: a few centimetres and 10-20 cm are what to expect of them.
* a static baseline of 2 hours around midday from the station to the partner (base.baseline: the partner as "your
  base", the station where the program puts it), the vector between the two against the difference of their SSC
  coordinates at the epoch: at most 0.02 m in plan and 0.04 m in height.
* every Hatanaka file of the two stations decoded by the program (rinex.plain: gzip, then the crx2rnx of the native
  core) and by the official CRX2RNX: the same bytes. One difference is known and told as such: CRX2RNX 4.2.0 writes a
  negative receiver clock offset of whole tenths of a millisecond one tenth nearer zero (tests/data/crinex/README.md).
"""
from __future__ import annotations

import argparse
import gzip
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

DEFAULT_STATION = "MEDI00ITA"  # Medicina (Bologna): EPN and IGS since 1996, its day at 30 s at BKG
DAYS_BACK = 21  # the day checked by default: the final products and the multi-year solution are out by then
PPP_LIMITS = (0.03, 0.06)  # m, plan and height: a static PPP of the whole day against the SSC coordinates
BASELINE_LIMITS = (0.02, 0.04)  # m, plan and height: a static baseline of 2 hours against the SSC difference
PARTNER_KM = (20.0, 60.0)  # how far the station of the baseline is, an EPN station
# the shorter sessions, hours of GPS time: around midday, when drones fly and the ionosphere is at its strongest
SHORT_SESSIONS = (("4 h", 10, 14), ("2 h", 11, 13))  # PPPs, printed only
BASELINE_HOURS = (11, 13)  # the static baseline
# the multi-year solution of the EPN in IGS20: those in ETRF2000, ETRF2014, ETRF2020 have the frame in the name, the
# stations with less than 3 years are in the _short file (positions only)
SSC_NAME = re.compile(r"EUR0OPSSNX_\d{7}_\d{7}_\d{2}[A-Z]_SOL\.SSC")
DISCONTINUITIES = "EPN_discontinuities.snx"
SINEX_EPOCH = re.compile(r"(\d\d):(\d{3}):(\d{5})")
# a row of SOLUTION/DISCONTINUITY: code, point, solution, observation code, from, to, P (position) or V (velocity)
DISCONTINUITY = re.compile(r"\s*(\w{4})\s+\S+\s+(\d+)\s+\w\s+(\d\d:\d{3}:\d{5})\s+(\d\d:\d{3}:\d{5})\s+([PV])\b")


def say(text: str = "") -> None:
    print(text, flush=True)


class CheckError(Exception):
    """A check that cannot be made: the message says why."""


# --- the truth: the multi-year solution of the EPN in IGS20 -----------------------------------------------------------


@dataclass(frozen=True)
class Solution:
    """One solution of a station in the SSC file: its number, the span of its data (decimal years, None for none),
    the position at the reference epoch and the velocity (m, m/yr), with their formal sigmas."""

    number: int
    data: tuple[float | None, float | None]
    ref: float
    xyz: tuple[float, float, float]
    sigma: tuple[float, float, float]
    velocity: tuple[float, float, float]
    sigma_v: tuple[float, float, float]


@dataclass(frozen=True)
class Truth:
    """The SSC coordinates of a station at an epoch (IGS20, m), their formal sigmas brought to it, the solution."""

    xyz: np.ndarray
    sigma: np.ndarray
    epoch: float
    solution: Solution
    why: str


def sinex_year(text: str) -> float | None:
    """The decimal year of a SINEX epoch (YY:DDD:SSSSS); None for 00:000:00000, which SINEX writes for no limit."""
    match = SINEX_EPOCH.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not a SINEX epoch")
    year, day, seconds = (int(v) for v in match.groups())
    if day == 0:
        return None
    year += 2000 if year < 50 else 1900
    length = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    return year + ((day - 1) * 86400 + seconds) / (length * 86400)


def epn_files(folder: Path) -> tuple[Path, Path, str]:
    """The SSC file of the multi-year solution of the EPN in IGS20 and the catalogue of its discontinuities, from the
    newest release (Cwwww folder) of the EPN Central Bureau that has both, downloaded into ``folder`` with the
    program's own calls; and the name of the release."""
    from pyarchinit_photogrammetry.gnss import download, stations

    root = stations.EPN_REFERENCE_FRAME
    releases = sorted((n.rstrip("/") for n in download.listing(root) if re.fullmatch(r"C\d{4}/", n)), reverse=True)
    for release in releases:
        names = download.listing(f"{root}{release}/")
        ssc = sorted(n for n in names if SSC_NAME.fullmatch(n))
        if ssc and DISCONTINUITIES in names:
            return (download.fetch(f"{root}{release}/{ssc[-1]}", folder / ssc[-1]),
                    download.fetch(f"{root}{release}/{DISCONTINUITIES}", folder / DISCONTINUITIES), release)
    raise CheckError(f"{root}: no release of the EPN has the multi-year solution in IGS20 and its discontinuities")


def read_ssc(path: Path) -> dict[str, list[Solution]]:
    """The solutions of every station of an SSC file of the EPN Central Bureau, by 4-character code. The layout (its
    README): a line of the position - DOMES, site name, GPS, code, X Y Z, their sigmas, the solution, DATA_START,
    DATA_END, the reference epoch - then a line of the velocity - DOMES, VX VY VZ, their sigmas."""
    found: dict[str, list[Solution]] = {}
    position = None
    with open(path, encoding="latin-1") as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 14 and fields[-12] == "GPS" and all(SINEX_EPOCH.fullmatch(v) for v in fields[-3:]):
                position = fields
            elif position is not None and len(fields) == 7 and fields[0] == position[0]:
                found.setdefault(position[-11].upper(), []).append(Solution(
                    int(position[-4]), (sinex_year(position[-3]), sinex_year(position[-2])), sinex_year(position[-1]),
                    tuple(float(v) for v in position[-10:-7]), tuple(float(v) for v in position[-7:-4]),
                    tuple(float(v) for v in fields[1:4]), tuple(float(v) for v in fields[4:7])))
                position = None
    if not found:
        raise CheckError(f"{path.name} has no station in the SSC layout of the EPN Central Bureau")
    return found


def read_discontinuities(path: Path) -> dict[str, list[tuple[int, float | None, float | None]]]:
    """The validity of the position solutions of every station in the catalogue of discontinuities of the EPN (SINEX
    SOLUTION/DISCONTINUITY): ``(solution, from, to)`` in decimal years, None for no limit."""
    found: dict[str, list[tuple[int, float | None, float | None]]] = {}
    inside = False
    with open(path, encoding="latin-1") as f:
        for line in f:
            if line.startswith("+SOLUTION/DISCONTINUITY"):
                inside = True
            elif line.startswith("-SOLUTION/DISCONTINUITY"):
                inside = False
            elif inside and (match := DISCONTINUITY.match(line)) and match.group(5) == "P":
                found.setdefault(match.group(1).upper(), []).append(
                    (int(match.group(2)), sinex_year(match.group(3)), sinex_year(match.group(4))))
    return found


class Reference:
    """The SSC coordinates of the stations at any epoch: the solution that the catalogue of discontinuities says is
    valid at the epoch, on the line of its position and velocity. A station the catalogue gives no solution valid at
    the epoch (not in it at all) is refused, as the program refuses it - never its last solution. The catalogue of a
    release knows the breaks up to its making (that of C2415 was made in 2026, day 246; latest/ holds the same file):
    a station moved later is on its old line until a newer release."""

    def __init__(self, ssc: Path, discontinuities: Path, release: str):
        self.ssc, self.release = ssc, release
        self.solutions = read_ssc(ssc)
        self.validity = read_discontinuities(discontinuities)

    def has(self, station) -> bool:
        return station.id[:4].upper() in self.solutions

    def at(self, station, epoch: float) -> Truth:
        code = station.id[:4].upper()
        solutions = self.solutions.get(code)
        if not solutions:
            raise CheckError(f"{station.id} is not in {self.ssc.name} ({self.release}): the stations with less than 3 "
                             "years of data, without a velocity, are not; nor are those with post-seismic models")
        valid = [s for s in self.validity.get(code, [])
                 if (s[1] is None or s[1] <= epoch) and (s[2] is None or epoch < s[2])]
        if not valid:  # the program refuses the station: so does the check
            absent = "" if code in self.validity else f" ({code} is not in it)"
            raise CheckError(f"{code} has no solution valid at {epoch:.4f} in {DISCONTINUITIES} of {self.release}"
                             f"{absent}: the program refuses such a station, and so does the check")
        number = max(valid)[0]
        chosen = next((s for s in solutions if s.number == number), None)
        if chosen is None:
            raise CheckError(f"{DISCONTINUITIES} puts {code} at {epoch:.4f} in solution {number}, which "
                             f"{self.ssc.name} does not hold: a break after the end of its data")
        why = f"solution {number}, valid at the epoch by {DISCONTINUITIES}"
        dt = epoch - chosen.ref
        xyz = np.array(chosen.xyz) + np.array(chosen.velocity) * dt
        if np.linalg.norm(xyz - np.array(station.xyz)) > 1000.0:
            raise CheckError(f"{self.ssc.name} has a station {code} more than 1 km from {station.id}: another one")
        sigma = np.hypot(np.array(chosen.sigma), np.array(chosen.sigma_v) * dt)
        return Truth(xyz, sigma, epoch, chosen, why)


# --- geometry and printing ------------------------------------------------------------------------------------------


def enu(delta, lat: float, lon: float) -> np.ndarray:
    """A difference of geocentric X, Y, Z (m) as east, north, up at ``lat``, ``lon`` (degrees)."""
    phi, lam = math.radians(lat), math.radians(lon)
    to_enu = np.array([[-math.sin(lam), math.cos(lam), 0.0],
                       [-math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi)],
                       [math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)]])
    return to_enu @ np.asarray(delta, dtype=np.float64)


def ecef(lat: float, lon: float, h: float) -> np.ndarray:
    from pyarchinit_photogrammetry.gnss import rinex

    return np.array(rinex.llh_to_ecef((lat, lon, h)))


def mm(values) -> str:
    return " ".join(f"{1000.0 * v:+8.1f}" for v in values) + " mm"


def difference(label: str, d: np.ndarray) -> tuple[float, float]:
    """Print the difference ``d`` (east, north, up) and return it in plan and in height."""
    plan = float(math.hypot(d[0], d[1]))
    say(f"    {label}: east north up {mm(d)}; plan {1000.0 * plan:.1f} mm, height {1000.0 * d[2]:+.1f} mm")
    return plan, float(d[2])


def truth_text(truth: Truth) -> str:
    s = truth.solution
    data = f"data {s.data[0]:.2f}-{s.data[1]:.2f}" if None not in s.data else "data"
    return (f"{truth.why} ({data}, reference epoch {s.ref:.1f}), at {truth.epoch:.4f}: X Y Z "
            f"{truth.xyz[0]:.4f} {truth.xyz[1]:.4f} {truth.xyz[2]:.4f} m, formal sigma {mm(truth.sigma)}")


# --- the data of the day ----------------------------------------------------------------------------------------------


def day_of(text: str) -> date:
    if text.strip():
        return date.fromisoformat(text.strip())
    return datetime.now(timezone.utc).date() - timedelta(days=DAYS_BACK)


def window(day: date) -> tuple[datetime, datetime]:
    """The hours whose observations, with the 10 minutes the program adds on each side (stations.MARGIN), are the file
    of the day: from 00:10 to 23:50 GPS time."""
    from pyarchinit_photogrammetry.gnss import stations

    midnight = datetime(day.year, day.month, day.day)
    return midnight + stations.MARGIN, midnight + timedelta(days=1) - stations.MARGIN


def observations(station, day: date, folder: Path) -> tuple[Path, list[Path]]:
    """The observations of the day of ``station`` at 30 s, as the program downloads, checks, decodes and joins them
    (stations.download_observations), and the files of the archive they came from, kept in the program's folder of
    the station as the archive names them."""
    from pyarchinit_photogrammetry.gnss import download, rinex, stations

    start, end = window(day)
    remote = stations.observation_files(station, start, end, rate="30s")
    path = stations.download_observations(station, start, end, folder, rate="30s")
    info = rinex.header(path)
    first, last = rinex.time_span(path)
    say(f"  {station.id}: {', '.join(r.url for r in remote)}")
    say(f"    RINEX {info.version:g}, {info.receiver}, antenna {' '.join(info.antenna.split())}, "
        f"height {info.antenna_delta_hen[0] if info.antenna_delta_hen else '-'} m, systems {''.join(info.systems)}, "
        f"epochs {first:%H:%M:%S}-{last:%H:%M:%S}")
    return path, [download.gnss_dir() / "stations" / station.id / r.name for r in remote]


def cut(source: Path, folder: Path, start: datetime, end: datetime) -> Path:
    """The epochs of the RINEX 3 file ``source`` from ``start`` up to ``end`` (GPS time, ``end`` left out), with its
    header, into a file of ``folder``: a session of some hours of the day. An event without a time goes with the epoch
    before it."""
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{source.name.split('_')[0]}_{start:%H%M}-{end:%H%M}.obs"
    keep = False
    with open(source, encoding="ascii", errors="surrogateescape", newline="") as src, \
            open(out, "w", encoding="ascii", errors="surrogateescape", newline="") as dst:
        for line in src:
            dst.write(line)
            if line[60:].startswith("END OF HEADER"):
                break
        for line in src:
            if line.startswith(">"):
                fields = line[1:].split()
                try:
                    when = datetime(*(int(v) for v in fields[:5])) + timedelta(seconds=float(fields[5]))
                except (ValueError, IndexError):
                    pass
                else:
                    keep = start <= when < end
            if keep:
                dst.write(line)
    return out


# --- the checks -------------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What a check found: its name, the numbers, and whether it passed (None: printed only)."""

    name: str
    text: str
    passed: bool | None


def choose_partner(main, pool, reference: Reference, day: date, wanted: str):
    """The station of the baseline: ``wanted``, or the nearest station of the EPN 20 to 60 km from ``main`` that has
    coordinates in the multi-year solution and a file of the day at 30 s (stations.nearby,
    stations.observation_files)."""
    from pyarchinit_photogrammetry.gnss import stations

    if wanted:
        partner = next((s for s in pool if s.id == wanted.upper()), None)
        if partner is None:
            raise CheckError(f"{wanted} is not in the program's list of stations")
        km = stations.nearby(main.lat, main.lon, limit=1, within_km=1e5, stations=[partner])[0][1]
        say(f"  {partner.id}, given, {km:.1f} km from {main.id}")
        return partner
    lo, hi = PARTNER_KM
    epn = [s for s in pool if "EPN" in s.networks and s.id != main.id]
    start, end = window(day)
    for station, km in stations.nearby(main.lat, main.lon, limit=100, within_km=hi, stations=epn):
        if km < lo:
            say(f"  {station.id} {km:.1f} km: closer than {lo:g} km")
            continue
        if not reference.has(station):
            say(f"  {station.id} {km:.1f} km: not in the multi-year solution {reference.release}")
            continue
        try:
            stations.observation_files(station, start, end, rate="30s")
        except ValueError as exc:
            say(f"  {station.id} {km:.1f} km: no file of the day at 30 s ({str(exc).splitlines()[0]})")
            continue
        say(f"  {station.id} ({station.name}), {km:.1f} km from {main.id}")
        return station
    raise CheckError(f"no station of the EPN {lo:g}-{hi:g} km from {main.id} has coordinates in the multi-year "
                     f"solution and a file of {day} at 30 s: give one with --partner")


def check_hatanaka(pieces: list[Path], crx2rnx: str, folder: Path) -> Outcome:
    """Every Hatanaka file of the archive decoded by the program (rinex.plain into a folder of its own: gzip, then
    the core's crx2rnx) and by the official CRX2RNX: the same bytes, or only the known misprint of CRX2RNX."""
    from pyarchinit_photogrammetry.gnss import rinex

    shutil.rmtree(folder, ignore_errors=True)
    (folder / "program").mkdir(parents=True)
    failed = []
    for piece in pieces:
        ours = rinex.plain(piece, folder / "program").read_bytes()
        crx = folder / piece.name[:-3]
        with gzip.open(piece, "rb") as packed, open(crx, "wb") as out:
            shutil.copyfileobj(packed, out)
        with open(crx, "rb") as stdin:  # an absolute path and no closing of descriptors, as the program spawns
            done = subprocess.run([str(Path(crx2rnx).resolve())], stdin=stdin, capture_output=True, close_fds=False)
        if done.returncode not in (0, 2):  # 2: a warning, the file is decoded
            raise CheckError(f"the official CRX2RNX could not decode {crx.name}: "
                             f"{done.stderr.decode(errors='replace').strip()}")
        theirs = done.stdout
        lines = ours.count(b"\n")
        if ours == theirs:
            say(f"  {piece.name}: {len(ours):,} bytes, {lines:,} lines, the same bytes as CRX2RNX")
            continue
        a, b = ours.split(b"\n"), theirs.split(b"\n")
        pairs = [(n, x, y) for n, (x, y) in enumerate(zip(a, b), start=1) if x != y]
        explained = len(a) == len(b) and all(misprint(x, y) for _n, x, y in pairs)
        say(f"  {piece.name}: {len(ours):,} bytes against {len(theirs):,}, {len(pairs)} lines differ"
            + (", all the known misprint of CRX2RNX 4.2.0 (a negative clock offset of whole tenths of a millisecond "
               "written one tenth nearer zero)" if explained else ""))
        for n, x, y in pairs[:3]:
            say(f"    line {n}: program  {x.decode(errors='replace')}")
            say(f"    line {n}: CRX2RNX  {y.decode(errors='replace')}")
        if not explained:
            failed.append(piece.name)
    if failed:
        return Outcome("Hatanaka", f"{len(failed)} of {len(pieces)} files differ from CRX2RNX: {', '.join(failed)}",
                       False)
    return Outcome("Hatanaka", f"{len(pieces)} files decoded as the official CRX2RNX 4.2.0 does", True)


def misprint(ours: bytes, theirs: bytes) -> bool:
    """Whether two epoch lines of RINEX 3 differ only by the receiver clock offset that CRX2RNX 4.2.0 misprints: ours
    negative and a whole number of tenths of a millisecond, its own one tenth of a millisecond nearer zero."""
    a, b = ours.split(), theirs.split()
    if not ours.startswith(b">") or len(a) != len(b) or len(a) < 9 or a[:-1] != b[:-1]:
        return False
    try:
        value, written = float(a[-1]), float(b[-1])
    except ValueError:
        return False
    return value < 0.0 and abs(value * 1e4 - round(value * 1e4)) < 1e-6 and abs(written - value - 1e-4) < 1e-12


def check_ppp(label: str, obs: Path, folder: Path, station, reference: Reference, products, limits) -> tuple:
    """The static PPP of ``obs`` (ppp.solve_static) against the SSC coordinates of ``station`` at its epoch."""
    from pyarchinit_photogrammetry.gnss import ppp

    say(f"  PPP {label}")  # before the run: the warnings the program logs come under it
    result = ppp.solve_static(obs, folder, products=products, atx="auto")
    p, detail = result.position, result.position.detail
    calibration = detail.get("antenna_calibration") or {}
    say(f"    session {detail['session'][0]} to {detail['session'][1]} GPS time, {result.hours:g} hours")
    say(f"    products: {result.products.centre} {result.products.kind}: {', '.join(detail['products'])}")
    say(f"    antenna {detail['antenna']}, height {(detail.get('antenna_delta_hen') or ['-'])[0]} m, calibration "
        f"{calibration.get('entry')!r} in {calibration.get('file')}"
        + (" (the type without the radome)" if calibration.get("without_radome") else ""))
    say(f"    RTKLIB: {detail['quality']} (Q={detail['q']}), {p.lat:.9f} {p.lon:.9f} {p.h:.4f} m, {p.frame} at "
        f"{p.epoch:.4f}; formal sigma east north up {mm(p.sd)}; {len(result.warnings)} warnings (logged above)")
    truth = reference.at(station, p.epoch)
    say(f"    SSC {station.id}: {truth_text(truth)}")
    plan, up = difference("PPP - SSC", enu(ecef(p.lat, p.lon, p.h) - truth.xyz, p.lat, p.lon))
    if limits is None:
        return result, Outcome(f"PPP {label}", f"plan {1000 * plan:.1f} mm, height {1000 * up:+.1f} mm "
                                               "(printed only)", None)
    ok = plan <= limits[0] and abs(up) <= limits[1]
    return result, Outcome(f"PPP {label}", f"plan {1000 * plan:.1f} mm (at most {1000 * limits[0]:g}), height "
                                           f"{1000 * up:+.1f} mm (at most {1000 * limits[1]:g})", ok)


def check_baseline(main, partner, main_day: Path, partner_day: Path, day: date, folder: Path,
                   reference: Reference) -> Outcome:
    """A static baseline of :data:`BASELINE_HOURS` from ``main``, where the program puts it, to ``partner`` (its
    observations as those of the user's own base: base.baseline): the vector between the two against the
    difference of their SSC coordinates at the epoch."""
    from pyarchinit_photogrammetry.gnss import base

    midnight = datetime(day.year, day.month, day.day)
    start, end = (midnight + timedelta(hours=h) for h in BASELINE_HOURS)
    sessions = folder / "sessions"
    station_obs, partner_obs = cut(main_day, sessions, start, end), cut(partner_day, sessions, start, end)
    say(f"  baseline {main.id} -> {partner.id}, {start:%H:%M}-{end:%H:%M} GPS time")  # the logged warnings under it
    result = base.baseline(partner_obs, main, folder / "baseline", station_obs=station_obs, atx="auto")
    p, detail = result.position, result.position.detail
    used = detail["station_position"]
    coordinates = used["detail"].get("coordinates") or {}
    say(f"    {result.hours:g} hours, {result.baseline_km:.3f} km, {detail['status']} (Q={result.q}), formal sigma "
        f"east north up {mm(result.sd)}; {len(result.warnings)} warnings (logged above)")
    say(f"    the program's position of {main.id}: from {coordinates.get('source')} ({used['frame']} at "
        f"{used['epoch']:.4f}), sigma {used['detail'].get('sigma')}"
        + (f" {mm(used['sd'])} east north up" if used.get("sd") else ""))
    epoch = p.epoch
    station_truth, partner_truth = reference.at(main, epoch), reference.at(partner, epoch)
    say(f"    SSC {main.id}: {truth_text(station_truth)}")
    say(f"    SSC {partner.id}: {truth_text(partner_truth)}")
    station_xyz, partner_xyz = ecef(used["lat"], used["lon"], used["h"]), ecef(p.lat, p.lon, p.h)
    difference(f"the program's {main.id} - SSC", enu(station_xyz - station_truth.xyz, used["lat"], used["lon"]))
    difference(f"{partner.id} by the baseline - SSC", enu(partner_xyz - partner_truth.xyz, p.lat, p.lon))
    plan, up = difference("vector - SSC difference",
                          enu((partner_xyz - station_xyz) - (partner_truth.xyz - station_truth.xyz), p.lat, p.lon))
    plan_limit, height_limit = BASELINE_LIMITS
    ok = plan <= plan_limit and abs(up) <= height_limit
    return Outcome("Baseline 2 h", f"{result.baseline_km:.1f} km, {detail['status']}: plan {1000 * plan:.1f} mm (at "
                                   f"most {1000 * plan_limit:g}), height {1000 * up:+.1f} mm (at most "
                                   f"{1000 * height_limit:g})", ok)


# --- the run ---------------------------------------------------------------------------------------------------------


def arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].rstrip(":") + ".",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--station", default=DEFAULT_STATION,
                        help=f"the station (9 characters, of the EPN); default {DEFAULT_STATION}")
    parser.add_argument("--day", default="", help=f"YYYY-MM-DD; empty: {DAYS_BACK} days ago")
    parser.add_argument("--partner", default="",
                        help="the station of the baseline; empty: the nearest station of the EPN 20-60 km away that "
                             "has coordinates in the multi-year solution and its day at 30 s")
    parser.add_argument("--crx2rnx", default="", help="the official CRX2RNX of RNXCMP; empty: CRX2RNX in the PATH")
    parser.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "ppg-gnss-check"),
                        help="the folder of the downloads and of the runs; default ppg-gnss-check in the temporary "
                             "folder of the system")
    return parser.parse_args(argv)


def run(args: argparse.Namespace, work: Path) -> list[Outcome]:
    from pyarchinit_photogrammetry import __version__, _native
    from pyarchinit_photogrammetry.gnss import base, frames, stations

    say(f"pyarchinit-photogrammetry {__version__}, native core {_native.version()} (API {_native.api()}), Python "
        f"{sys.version.split()[0]}; GNSS folder {os.environ['PPG_GNSS_DIR']}")
    if not (_native.supports("ppk") and _native.supports("crinex")):
        raise CheckError("the native core with RTKLIB and crx2rnx (0.6 or later) is not installed, or PPG_NATIVE is 0")
    crx2rnx = args.crx2rnx or shutil.which("CRX2RNX")
    if not crx2rnx:
        raise CheckError("no official CRX2RNX: give --crx2rnx (RNXCMP 4.2.0, https://terras.gsi.go.jp/ja/crx2rnx.html)")
    day = day_of(args.day)
    say(f"Day {day} (day {day.timetuple().tm_yday} of {day.year}, GPS week {(day - date(1980, 1, 6)).days // 7})")

    say("\nThe multi-year solution of the EPN (the truth)")
    reference = Reference(*epn_files(work / "epn"))
    say(f"  {reference.release}: {reference.ssc.name}, {len(reference.solutions)} stations; {DISCONTINUITIES}")

    say("\nThe stations")
    pool = stations.station_list()
    main = next((s for s in pool if s.id == args.station.upper()), None)
    if main is None:
        raise CheckError(f"{args.station} is not in the program's list of stations")
    say(f"  {main.id} ({main.name}), {', '.join(main.networks)}, {main.lat:.4f} {main.lon:.4f}")
    noon = frames.decimal_year(datetime(day.year, day.month, day.day, 12))
    reference.at(main, noon)  # a station that is not in the multi-year solution cannot be checked: says why
    partner = choose_partner(main, pool, reference, day, args.partner)

    say(f"\nTheir coordinates at noon ({noon:.4f}): the SSC, and the program's (stations.coordinates)")
    for station in (main, partner):
        truth = reference.at(station, noon)
        say(f"  SSC {station.id}: {truth_text(truth)}")
        given = stations.coordinates(station, noon)
        say(f"    the program's: from {given.source}, sigma {base.SIGMA_KINDS.get(given.source)}"
            + (f" {mm(given.sigma)} X Y Z" if given.sigma is not None else ""))
        difference("program - SSC", enu(np.array(given.xyz) - truth.xyz, *given.llh[:2]))

    say("\nTheir day at 30 s, downloaded by the program")
    main_day, main_pieces = observations(main, day, work / "obs")
    partner_day, partner_pieces = observations(partner, day, work / "obs")

    outcomes: list[Outcome] = []

    def attempt(name: str, check):
        try:
            return check()
        except Exception as exc:  # one check that cannot be made does not stop the others
            traceback.print_exc(file=sys.stdout)
            outcomes.append(Outcome(name, f"{type(exc).__name__}: {str(exc).splitlines()[0]}", False))
            return None

    say("\nHatanaka: the program against the official CRX2RNX")
    found = attempt("Hatanaka", lambda: check_hatanaka(main_pieces + partner_pieces, crx2rnx, work / "hatanaka"))
    if found:
        outcomes.append(found)

    say(f"\nStatic PPP of {main.id}")
    whole = attempt("PPP 24 h", lambda: check_ppp("24 h", main_day, work / "ppp_24h", main, reference, None,
                                                  PPP_LIMITS))
    if whole:
        outcomes.append(whole[1])
        midnight = datetime(day.year, day.month, day.day)
        for label, a, b in SHORT_SESSIONS:
            session = cut(main_day, work / "sessions", midnight + timedelta(hours=a), midnight + timedelta(hours=b))
            short = attempt(f"PPP {label}", lambda: check_ppp(label, session, work / f"ppp_{label.replace(' ', '')}",
                                                              main, reference, whole[0].products, None))
            if short:
                outcomes.append(short[1])

    say(f"\nStatic baseline {main.id} -> {partner.id}")
    found = attempt("Baseline 2 h", lambda: check_baseline(main, partner, main_day, partner_day, day, work,
                                                           reference))
    if found:
        outcomes.append(found)
    return outcomes


def report(outcomes: list[Outcome], title: str) -> None:
    """The outcomes in the log and, on GitHub, in the summary of the run."""
    marks = {True: "PASS", False: "FAIL", None: "-"}
    say(f"\nSummary: {title}")
    for outcome in outcomes:
        say(f"  {marks[outcome.passed]:4}  {outcome.name:14}  {outcome.text}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {title}\n\n| check | result | |\n|---|---|---|\n")
            f.writelines(f"| {o.name} | {o.text} | {marks[o.passed]} |\n" for o in outcomes)


def main(argv=None) -> int:
    args = arguments(argv)
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    # before the program is imported: its downloads in a folder of the run, never the program's own
    os.environ.setdefault("PPG_GNSS_DIR", str(work / "gnss"))
    os.environ.setdefault("GLOG_minloglevel", "2")
    logging.basicConfig(level=logging.WARNING, format="    [%(name)s] %(message)s", stream=sys.stdout)
    try:
        outcomes = run(args, work)
    except Exception as exc:  # a station, a day or an archive that cannot be had: no check at all
        if not isinstance(exc, CheckError):
            traceback.print_exc(file=sys.stdout)
        say(f"\nThe check could not be made: {exc}")
        return 1
    report(outcomes, f"GNSS check of {args.station.upper()} on {day_of(args.day)}")
    return 0 if outcomes and all(o.passed is not False for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
