"""Camera positions and their accuracy from a text file: a drone built by hand, positions computed by another
program (a PPK solution, a total station, a flight log exported to CSV)."""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path, PurePath

from ..core import crs as crs_mod
from ..core.chunk import Chunk
from . import dji, ppk
from .apply import FACTOR, accuracy, set_camera_position

SOURCE = "positions_file"
HEIGHTS = ("ellipsoidal", "exif")

# the names a column can have in the header (lower case, units and spaces removed), by field
FIELDS = {
    "label": {"name", "label", "image", "photo", "file", "filename", "image_name", "photo_name", "camera", "foto",
              "immagine", "nome"},
    "x": {"x", "lon", "long", "lng", "longitude", "longitudine", "east", "easting"},
    "y": {"y", "lat", "latitude", "latitudine", "north", "northing"},
    "z": {"z", "alt", "altitude", "altitudine", "h", "height", "ellh", "elevation", "elev", "quota"},
    "sx": {"sx", "stdx", "std_x", "sd_x", "sigma_x", "sde", "std_e", "std_east", "std_easting", "std_lon"},
    "sy": {"sy", "stdy", "std_y", "sd_y", "sigma_y", "sdn", "std_n", "std_north", "std_northing", "std_lat"},
    "sz": {"sz", "stdz", "std_z", "sd_z", "sigma_z", "sdu", "std_u", "std_up", "std_h", "std_hgt", "std_height",
           "std_alt", "std_ellh"},
    "std": {"std", "sd", "sigma", "stdev", "stddev"},
    "accuracy": {"accuracy", "acc", "accuratezza", "precisione"},
    "yaw": {"yaw", "heading"},
    "pitch": {"pitch"},
    "roll": {"roll"},
    "status": {"status", "q", "flag", "rtk_flag", "rtkflag", "quality"},
}
_FIELD_OF = {name: field for field, names in FIELDS.items() for name in names}
_ANGLES = ("yaw", "pitch", "roll")

# a number in the status column is DJI's RtkFlag (50, 34, 16, 0) or RTKLIB's quality Q (ppk.status); the words are
# RTKLIB's names of its Q, as ppk.status counts them (dgps float, sbas and ppp single), and the words of others
_STATUS_WORDS = {**{name: ppk.status(q) for q, name in ppk.QUALITY.items()}, "fixed": "fix", "rtkfix": "fix",
                 "rtkfixed": "fix", "rtkfloat": "float", "autonomous": "single", "standalone": "single",
                 "none": "none", "nofix": "none"}
_AUTO = object()  # the separator is not known yet


def read_positions(path: str | Path, delimiter: str | None = None) -> list[dict]:
    """The positions in a text file, one dict per line: ``label`` (the photo, with or without its extension, and
    with its folder if the name is not enough), ``x``, ``y``, ``z`` (the order of the coordinate system: longitude,
    latitude, height; east, north, height) and, when the line has them, ``sx``, ``sy``, ``sz`` (standard
    deviations in metres) or ``accuracy`` (metres, already an accuracy), ``yaw``, ``pitch``, ``roll`` (degrees)
    and ``status`` (``"fix"``, ``"float"``, ``"single"``, ``"none"`` or ``"unknown"``).

    The file is a table of lines of text with or without a header. A header names the columns, in any order and
    with units after them: ``name|label|image|photo``, ``x|lon|longitude|easting``, ``y|lat|latitude|northing``,
    ``z|alt|h|height|ellh``, ``sx|std_x|sde|...`` (``sx``, ``sy`` and ``sz`` together), ``std`` (one standard
    deviation for the three axes), ``accuracy``, ``yaw``, ``pitch``, ``roll`` and ``status|q|flag`` (words, the
    DJI ``RtkFlag`` 50/34/16/0 or RTKLIB's quality Q: 1 fix, 2 float, 4 dgps as float, any other single, as for a
    PPK solution). Without a header the columns are ``label,x,y,z`` or ``label,x,y,z,sx,sy,sz``. The header may also
    be the first comment.

    Fields are separated by a tab, a semicolon or a comma (the first of them in the first line), or by spaces
    when there is none of those; ``delimiter`` forces one (spaces for runs of spaces; a tab or any
    other character separates exactly, so an empty cell is a column). With a delimiter other than the comma, ``,``
    is a decimal comma. Lines starting with ``#``, empty lines and lines of separators alone are skipped. Anything
    wrong in the file - a number that is not one, a missing column - raises ``ValueError`` naming the line."""
    path = Path(path)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:  # a spreadsheet saved in the Windows code page
        text = data.decode("cp1252", errors="replace")
    sep = _AUTO if delimiter is None else (None if not delimiter.strip(" ") else delimiter)  # a tab is a separator
    fields: dict[str, int] | None = None
    rows: list[dict] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip(" ")  # not the tabs: an empty first cell is a column
        comment = line.startswith("#")
        if comment:
            line = line.lstrip("#").strip(" ")
        if not line:
            continue
        use = _separator(line) if sep is _AUTO else sep
        tokens = _split(line, use)
        if not any(tokens):  # a line of separators alone, as a spreadsheet leaves at the end
            continue
        where = f"{path.name}:{number}"
        if comment:  # the header may be the first comment; any other comment is skipped
            try:
                header = _header(tokens, where) if fields is None else None
            except ValueError:
                header = None
            if header is not None:
                fields, sep = header, use
            continue
        if fields is None:
            header = _header(tokens, where)
            fields, sep = header or _bare_columns(tokens, where), use
            if header is not None:
                continue
        rows.append(_row(tokens, fields, use != ",", where))
    if not rows:
        raise ValueError(f"{path.name}: no positions found")
    return rows


def apply_positions(chunk: Chunk, rows: list[dict], crs: str, height: str = "ellipsoidal",
                    default_accuracy: tuple[float, float, float] = (0.05, 0.05, 0.10)) -> dict:
    """Give the photos of ``chunk`` the positions in ``rows`` (from :func:`read_positions`), which are in the
    coordinate system ``crs`` (``"EPSG:4979"`` for longitude, latitude and ellipsoidal height, a projected system,
    or ``"LOCAL"``) with heights of the kind ``height`` (``"ellipsoidal"`` or ``"exif"``).

    A row is paired with a photo by the name of its file, with or without the extension and in any case; when the
    name is not enough (the same name in two folders, ``DJI_0001.JPG`` in ``100MEDIA`` and ``101MEDIA``) the label
    carries the folder: ``100MEDIA/DJI_0001``. A label that fits several photos, or two rows for one photo, are
    refused before anything changes. The position replaces whatever the photo had, so doing it again changes nothing.

    The accuracy of a row is its ``accuracy`` as given, else twice its standard deviations ``sx``, ``sy``, ``sz``
    as for the RTK of DJI photos (and never below the floor of its ``status``, when it has one), else
    ``default_accuracy`` (also raised to the floor of the ``status`` of the row: 0.01, 0.20, 1 m for fix, float,
    single, 5/5/10 m for none). Returns ``{"matched", "rows", "unmatched": [label]}``: ``unmatched`` are the rows whose
    photo is not in the chunk."""
    if height not in HEIGHTS:
        raise ValueError(f"height must be one of {', '.join(HEIGHTS)}, not '{height}'")
    if not crs_mod.is_local(crs):
        try:
            geographic = crs_mod.is_geographic(crs)
        except Exception as exc:
            raise ValueError(f"unknown coordinate system '{crs}'") from exc
    else:
        geographic = False
    default = [float(a) for a in default_accuracy]
    if len(default) != 3 or any(not a > 0 for a in default):
        raise ValueError("default_accuracy needs three accuracies above 0")

    index = ({}, {})  # the cameras by lower case file name and by name without extension
    for cam in chunk.cameras:
        parts = _parts(Path(cam.path))
        index[0].setdefault(parts[-1], []).append((cam, parts))
        index[1].setdefault(Path(parts[-1]).stem, []).append((cam, parts))
    plan: dict[int, tuple] = {}
    unmatched = []
    for row in rows:
        label = str(row["label"]).strip()
        found = _find(index, label)
        if not found:
            unmatched.append(label)
            continue
        if len(found) > 1:
            names = ", ".join(sorted(str(Path(c.path)) for c in found))
            raise ValueError(f"'{label}' matches {len(found)} photos ({names}): give the folder too, as in "
                             "100MEDIA/DJI_0001.JPG")
        cam = found[0]
        if cam.key in plan:
            raise ValueError(f"photo '{cam.label}' appears twice in the positions ('{plan[cam.key][0]}' and "
                             f"'{label}')")
        x, y, z = float(row["x"]), float(row["y"]), float(row["z"])
        if geographic and not (abs(x) <= 360.0 and abs(y) <= 90.0):
            raise ValueError(f"'{label}': latitude {y} / longitude {x} is out of range in {crs} (swapped?)")
        status = row.get("status")
        acc, extra = _accuracy_of(row, status, default)
        extra.update({a: float(row[a]) for a in _ANGLES if a in row})
        plan[cam.key] = (label, cam, [x, y, z], acc, status, extra)
    for _label, cam, location, acc, status, extra in plan.values():
        set_camera_position(cam, location, crs, acc, SOURCE, status=status, height=height, extra=extra)
    return {"matched": len(plan), "rows": len(rows), "unmatched": unmatched}


def _accuracy_of(row: dict, status: str | None, default: list[float]) -> tuple[list[float], dict]:
    """``(accuracy, what to add to the record)`` of a row."""
    if "accuracy" in row:
        return [float(row["accuracy"])] * 3, {}
    if all(k in row for k in ("sx", "sy", "sz")):
        std = [float(row["sx"]), float(row["sy"]), float(row["sz"])]
        return (accuracy(status, std) if status else [FACTOR * s for s in std]), {"std": std}
    if status:  # a declared status is never more accurate than its floor
        return [max(d, f) for d, f in zip(default, accuracy(status, None))], {}
    return list(default), {}


def _parts(path: PurePath) -> tuple[str, ...]:
    """The folders and the file name of ``path`` in lower case, split as :func:`_find` splits a label: the root goes,
    a drive is ``c:`` and a network share is its server and its share."""
    return tuple(p for part in path.parts for p in re.split(r"[\\/]+", part.lower()) if p)


def _find(index: tuple[dict, dict], label: str) -> list:
    """The cameras that ``label`` names: the file name as it is, else without the extension, under the folders the
    label gives. ``index`` holds ``(camera, lower case parts of its path)`` by file name and by name without
    extension."""
    want = [p for p in re.split(r"[\\/]+", label.lower()) if p]
    if not want:
        return []
    *folders, last = want

    def under(items):
        return [c for c, parts in items if not folders or parts[-1 - len(folders):-1] == tuple(folders)]

    return under(index[0].get(last, ())) or under(index[1].get(last, ()))


def _separator(line: str) -> str | None:
    """The separator of a line of the file: a tab, a semicolon or a comma, else (None) runs of spaces."""
    return next((d for d in ("\t", ";", ",") if d in line), None)


def _split(line: str, sep: str | None) -> list[str]:
    if sep is None:
        return line.split()
    return [t.strip() for t in next(csv.reader([line], delimiter=sep))]


def _name(token: str) -> str:
    token = re.sub(r"[(\[{].*?[)\]}]", "", token)
    return re.sub(r"[\s.\-]+", "_", token.strip().lower()).strip("_")


def _is_number(token: str) -> bool:
    try:
        float(token.replace(",", "."))
    except ValueError:
        return False
    return True


def _header(tokens: list[str], where: str) -> dict[str, int] | None:
    """The columns named by a header line, None when the line is not a header (no column has a known name, or a
    cell is a number); ``ValueError`` when it is one but something is missing."""
    fields: dict[str, int] = {}
    for i, token in enumerate(tokens):
        field = _FIELD_OF.get(_name(token))
        if field is not None:
            if field in fields:
                raise ValueError(f"{where}: two columns are the {field}")
            fields[field] = i
    if not fields or any(_is_number(t) for t in tokens):
        return None
    for field, name, columns in (("label", "name", "name, label, image or photo"), ("x", "x", "x, lon or easting"),
                                 ("y", "y", "y, lat or northing"), ("z", "height", "z, alt, height or ellh")):
        if field not in fields:
            raise ValueError(f"{where}: the header has no {name} column ({columns})")
    if len({"sx", "sy", "sz"} & fields.keys()) not in (0, 3):
        raise ValueError(f"{where}: the header needs sx, sy and sz together")
    return fields


def _bare_columns(tokens: list[str], where: str) -> dict[str, int]:
    names = ["label", "x", "y", "z", "sx", "sy", "sz"]
    if len(tokens) not in (4, 7):
        raise ValueError(f"{where}: {len(tokens)} columns without a header: expected label, x, y, z or label, x, "
                         "y, z, sx, sy, sz")
    return {name: i for i, name in enumerate(names[:len(tokens)])}


def _row(tokens: list[str], fields: dict[str, int], decimal_comma: bool, where: str) -> dict:
    tokens = tokens + [""] * (max(fields.values()) + 1 - len(tokens))

    def cell(field: str) -> str:
        return tokens[fields[field]] if field in fields else ""

    def number(field: str) -> float:
        text = cell(field)
        try:
            value = float(text.replace(",", ".") if decimal_comma else text)
        except ValueError:
            raise ValueError(f"{where}: {field} '{text}' is not a number") from None
        if not math.isfinite(value):
            raise ValueError(f"{where}: {field} '{text}' is not a finite number")
        return value

    def positive(field: str) -> float:
        value = number(field)
        if not value > 0:
            raise ValueError(f"{where}: the accuracy given in {field} must be above 0, not {cell(field)}")
        return value

    if not cell("label"):
        raise ValueError(f"{where}: the line has no name")
    row = {"label": cell("label"), "x": number("x"), "y": number("y"), "z": number("z")}
    spread = [f for f in ("sx", "sy", "sz") if f in fields]
    if spread and any(cell(f) for f in spread):
        blank = [f for f in spread if not cell(f)]
        if blank:
            raise ValueError(f"{where}: sx, sy and sz come together, {blank[0]} is empty")
        row.update({f: positive(f) for f in spread})
    elif cell("std"):
        std = positive("std")
        row.update(sx=std, sy=std, sz=std)
    if cell("accuracy"):
        row["accuracy"] = positive("accuracy")
    for angle in _ANGLES:
        if cell(angle):
            row[angle] = number(angle)
    if cell("status"):
        try:
            row["status"] = _status(cell("status"))
        except OverflowError:  # a number too large to be a flag: inf
            raise ValueError(f"{where}: status '{cell('status')}' is not a status") from None
    return row


def _status(text: str) -> str:
    """``"fix"``, ``"float"``, ``"single"``, ``"none"`` or ``"unknown"`` for the status of a position: a word, DJI's
    RtkFlag, or RTKLIB's quality Q for any other number, as :func:`.ppk.status` counts it."""
    try:
        number = int(float(text))
    except ValueError:
        return _STATUS_WORDS.get(re.sub(r"[\s_\-]+", "", text.lower()), "unknown")
    return dji.STATUS.get(number) or ppk.status(number)
