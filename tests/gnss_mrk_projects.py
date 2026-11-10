"""Three DJI projects whose Timestamp.MRK and PPK results are pinned in ``data/gnss/phase4/mrk_behaviour.json``: the
photos paired by shot number (a photo without a line, a shot without a photo, a photo off time, an onboard fix in the
XMP), by order (and the refusal when the counts differ), and a PPK flight (shots before, between and after the epochs of
the solution, the onboard fixes kept or replaced, a far MRK, a solution of another day). The file was written by the
code of ``apply_mrk`` and ``apply_ppk`` before shots of any source were paired (``python tests/gnss_mrk_projects.py``
with the tests folder on the path); ``test_gnss_shots.test_mrk_behaviour_unchanged`` builds the projects again and
compares."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import dji, ppk
from gnss_helpers import START_2005, WEEK, drone_rows, jpeg, mrk_line, pos_text, ppk_flight, snapshot

SNAPSHOT = Path(__file__).parent / "data" / "gnss" / "phase4" / "mrk_behaviour.json"


def _xmp(body: str) -> str:
    return ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" {body}'
            '</rdf:Description></rdf:RDF></x:xmpmeta>')


def _line(index: int, utc: datetime, arm, lat: float, flag: int, std=(0.012, 0.011, 0.022)) -> str:
    gps = (utc - dji.GPS_EPOCH).total_seconds() + dji.LEAP_SECONDS
    week, seconds = divmod(gps, WEEK)
    north, east, down = arm
    return (f"{index}\t{seconds:.6f}\t[{int(week)}]\t{north:6d},N\t{east:6d},E\t{down:6d},V\t{lat:.8f},Lat\t"
            f"14.26600034,Lon\t94.350,Ellh\t{std[0]}, {std[1]}, {std[2]}\t{flag},Q\n")


def _state(chunk, tmp: Path) -> dict:
    return _plain({"cameras": snapshot(chunk), "memory": chunk.meta.get(ppk.MEMORY)}, tmp)


def _plain(value, tmp: Path):
    """``value`` as JSON gives it back, the folder of the run written ``<tmp>``."""
    return json.loads(json.dumps(value, default=str).replace(str(tmp), "<tmp>"))


def _attempt(call, tmp: Path):
    try:
        return {"result": _plain(call(), tmp)}
    except ValueError as exc:
        return {"error": str(exc).replace(str(tmp), "<tmp>").replace("\\", "/")}  # one spelling of the paths on Windows too


def build(tmp: Path) -> dict:
    """Run ``apply_mrk`` and ``apply_ppk`` on the three projects in ``tmp``: what they return (or the text of their
    error) and the state of the photos after each run."""
    out = {}
    # 1. by shot number: photos 1-5 of a Mavic 3 in Italy (UTC+2); the MRK has shots 2-6; photo 3 is 10 s late
    folder = tmp / "numbered"
    folder.mkdir()
    start = datetime(2024, 5, 12, 8, 30, 15, 400000, tzinfo=timezone.utc)
    flags = {2: 50, 3: 34, 4: 16, 5: 50, 6: 0}
    arms = {2: (120, -330, 189), 3: (0, 0, 150), 4: (-2, 11, 189), 5: (35, -40, 170), 6: (5, 5, 150)}
    for i in range(1, 6):
        late = 10 if i == 3 else 0
        when = (start + timedelta(seconds=2 * i + late, hours=2)).replace(tzinfo=None)
        xmp = _xmp('drone-dji:RtkFlag="50" drone-dji:RtkStdLon="0.004" drone-dji:RtkStdLat="0.004" '
                   'drone-dji:RtkStdHgt="0.009">') if i == 1 else None
        jpeg(folder / f"DJI_20240512103015_{i:04d}_V.JPG", xmp, when=when)
    (folder / "Timestamp.MRK").write_text("".join(
        _line(i, start + timedelta(seconds=2 * i), arms[i], 41.134 + 1e-5 * i, flags[i]) for i in range(2, 7)))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    out["numbered"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)
    out["numbered_state"] = _state(ch, tmp)
    out["numbered_again"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)

    # 2. by order: names without a shot number, no dates; then a line more than photos
    folder = tmp / "order"
    folder.mkdir()
    for name in ("a", "b", "c", "d"):
        jpeg(folder / f"{name}.JPG", None)
    lines = [_line(i, start + timedelta(seconds=3 * i), (10 * i, -5 * i, 150), 41.2 + 1e-5 * i, (50, 34, 16, 50)[i - 1])
             for i in range(1, 5)]
    (folder / "Timestamp.MRK").write_text("".join(lines))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    out["order"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)
    out["order_state"] = _state(ch, tmp)
    (folder / "Timestamp.MRK").write_text("".join(lines) + _line(5, start + timedelta(seconds=15), (0, 0, 150), 41.2,
                                                                  50))
    out["order_refused"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)

    # 3. PPK on the day of the RTKLIB sample data
    pos = tmp / "drone.pos"
    pos.write_text(pos_text(rows=drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    shots = [(1, START_2005 - 30.0, (-2, 11, 189)), (2, START_2005, (120, -330, 189)),
             (3, START_2005 + 30.0004, (0, 0, 150)), (4, START_2005 + 75.0, (35, -40, 170)),
             (5, START_2005 + 90.0, (-60, 25, 160)), (6, START_2005 + 120.0, (80, 90, 140)),
             (7, START_2005 + 150.4, (-15, 200, 180)), (8, START_2005 + 3570.0, (10, -10, 175)),
             (9, START_2005 + 3580.0, (5, 5, 150))]
    for name, lat, replace in (("ppk", None, False), ("ppk_replace", None, True), ("ppk_far", 35.1335, False)):
        folder = ppk_flight(tmp / name, shots, **({"lat": lat} if lat else {}))
        if name != "ppk_far":  # shots 2 and 6 an onboard fix, 3 a float
            text = (folder / "Timestamp.MRK").read_text().splitlines(keepends=True)
            text[1] = text[1].replace("16,Q", "50,Q")
            text[2] = text[2].replace("16,Q", "34,Q")
            text[5] = text[5].replace("16,Q", "50,Q")
            (folder / "Timestamp.MRK").write_text("".join(text), newline="\n")
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        out[f"{name}_mrk"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)
        out[name] = _attempt(lambda: gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution, replace_fix=replace),
                             tmp)
        out[f"{name}_state"] = _state(ch, tmp)
    # an MRK of the day after: not the flight of the solution
    folder = tmp / "other_day"
    folder.mkdir()
    for i in (1, 2):
        jpeg(folder / f"DJI_{i:04d}.JPG", None)
    (folder / "Timestamp.MRK").write_text("".join(mrk_line(i, START_2005 + 86400.0 + 5 * i, (0, 0, 150))
                                                  for i in (1, 2)), newline="\n")
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    out["other_day"] = _attempt(lambda: gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution), tmp)
    # an empty Timestamp.MRK beside DJI photos, and beside photos without a shot number
    for name, photos in (("empty", ("DJI_0001.JPG", "DJI_0002.JPG")), ("empty_names", ("a.JPG", "b.JPG"))):
        folder = tmp / name
        folder.mkdir()
        for photo in photos:
            jpeg(folder / photo, None)
        (folder / "Timestamp.MRK").write_text("")
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        out[f"{name}_mrk"] = _attempt(lambda: gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK"), tmp)
        out[f"{name}_ppk"] = _attempt(lambda: gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution), tmp)
        out[f"{name}_state"] = _state(ch, tmp)
    return out


if __name__ == "__main__":  # write the snapshot (only with the code it pins)
    import tempfile

    with tempfile.TemporaryDirectory(dir=sys.argv[1] if len(sys.argv) > 1 else None) as where:
        result = build(Path(where).resolve())
    SNAPSHOT.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(SNAPSHOT, len(result))
