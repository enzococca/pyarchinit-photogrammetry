"""PPK smoke test of the native core: RTKLIB inside ppg_core (ppk_solve) on RTKLIB's test data, the
station 3040 against 0759 (tests/data/rtklib; tests/test_native.py has the same runs). The native and
the windows-arm workflows run it after the wheel of the core is installed, each on the platforms it
builds for, and the desktop workflow on the core of the Windows ARM64 app: python .github/scripts/ppk_smoke.py

Standing still every epoch is fixed and the station is where its RINEX header puts it: that run is
the proof of accuracy. Moving, how many epochs reach a fix hangs on the last bit of the arithmetic
and of each system's maths library (39-49 of 120, 37-115 at the extremes) and fix-and-hold keeps a
wrong fix now and then: a tenth fixed at least, and the median of those on the static station.
"""
import pathlib
import tempfile

import numpy as np
import ppg_core

data, tmp = pathlib.Path(__file__).resolve().parents[2] / "tests" / "data" / "rtklib", pathlib.Path(tempfile.mkdtemp())
files = [str(data / name) for name in ("30400920.05o", "07590920.05o", "07590920.05n")]


def solve(mode):  # the fixed rows (week, tow, lat, lon, h, Q) of a solution of every epoch
    conf, out = tmp / f"{mode}.conf", tmp / f"{mode}.pos"
    conf.write_text(f"pos1-posmode={mode}\npos1-frequency=l1+l2\npos1-soltype=combined\npos1-navsys=1\n"
                    "pos2-armode=fix-and-hold\nant2-postype=rinexhead\nout-timeform=tow\n")
    code, message = ppg_core.ppk_solve(str(conf), files, str(out))
    print(mode, "RTKLIB code", code, "-", message)  # before the file is read: a missing one is explained here
    assert code == 0, (mode, code, message)
    rows = np.array([r.split()[:6] for r in out.read_text().splitlines() if r.strip() and not r.startswith("%")],
                    float).reshape(-1, 6)
    print(mode, len(rows), "epochs,", int((rows[:, 5] == 1).sum()), "fixed")
    assert len(rows) == 120, (mode, len(rows))
    return rows[rows[:, 5] == 1]


def offsets(fixed, ref):  # north, east, up (m) from ref (lat, lon, h)
    k = np.pi / 180 * 6378137.0
    return (fixed[:, 2] - ref[0]) * k, (fixed[:, 3] - ref[1]) * k * np.cos(np.radians(ref[0])), fixed[:, 4] - ref[2]


def ecef(lat_deg, lon_deg, h):  # WGS84
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    n = 6378137.0 / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.array([(n + h) * np.cos(lat) * np.cos(lon), (n + h) * np.cos(lat) * np.sin(lon), (n * (1 - e2) + h) * np.sin(lat)])


static = solve("static")
assert len(static) > 96, len(static)
station = static[:, 2:5].mean(axis=0)
n, e, u = offsets(static, station)
h, v = np.sqrt(np.mean(n ** 2 + e ** 2)), np.std(u)
print(f"static: {100 * h:.2f} cm horizontal RMS, {100 * v:.2f} cm vertical std")
assert h < 0.03 and v < 0.06
# and where the station is: the position in its RINEX header, give or take the metres of the base's
header = [line for line in (data / "30400920.05o").read_text().splitlines() if "APPROX POSITION XYZ" in line][0]
away = np.linalg.norm(ecef(*station) - np.array(header[:42].split(), float))
print(f"static: {away:.2f} m from the position in the RINEX header")
assert away < 10.0, away
moving = solve("kinematic")
assert len(moving) >= 12, len(moving)
median = np.median(np.abs(offsets(moving, station)), axis=1)
print("kinematic, fixed epochs from the static station: median", np.round(100 * median, 2), "cm north, east, up")
assert (median < [0.02, 0.02, 0.03]).all(), median
