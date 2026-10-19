"""Excavation over time: the phases of an excavation are the chunks of a project, one per
survey (a day, the end of a US), in one frame (a common georeference, or *Allinea chunk*).

* :func:`phase_date` - the date of a phase: ``chunk.meta["date"]`` when set (:func:`set_phase_date`),
  else the earliest date its photos were taken (EXIF).
* :func:`phases` - the chunks with a DEM, in date order (undated ones last, in project order).
* :func:`removed` - from each phase to the next: the volume removed and added where both DEMs
  are (or inside a polygon), and for every unit drawn on the earlier phase - exposed then, and dug
  away before the next survey - the volume removed inside it.
* :func:`suggested_relations` - a unit exposed in an earlier phase that overlaps in plan a unit
  exposed in a later one lay above it: ``Copre`` / ``Coperto da`` in PyArchInit's terms, as
  suggestions for the US forms.
* :func:`timeline` - all of it, for the interface, the command line and :func:`timeline_csv`.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from ..core.progress import Progress, as_progress

MIN_OVERLAP_M2 = 0.01  # touching outlines are not a relation
MIN_OVERLAP_SHARE = 0.05  # of the smaller unit


def phase_date(chunk) -> datetime | None:
    """When the phase was surveyed (see the module docstring)."""
    d = (chunk.meta or {}).get("date")
    if d:
        try:
            return datetime.fromisoformat(str(d))
        except ValueError:
            pass
    dates = []
    for cam in chunk.cameras:
        s = (cam.meta or {}).get("datetime")
        if not s:
            continue
        try:
            dates.append(datetime.strptime(str(s).strip()[:19], "%Y:%m:%d %H:%M:%S"))
        except ValueError:
            continue
    return min(dates) if dates else None


def set_phase_date(chunk, date: datetime | str | None) -> None:
    """The date of the phase by hand (``None``: back to the date of the photos)."""
    if date is None or date == "":
        chunk.meta.pop("date", None)
    else:
        chunk.meta["date"] = (date if isinstance(date, datetime) else datetime.fromisoformat(str(date))).isoformat(
            timespec="minutes")


def phases(doc) -> list:
    """The chunks with a DEM in date order (undated ones last, in the order of the project)."""
    chunks = [ch for ch in doc.chunks if ch.dem_path is not None and ch.dem_path.exists()]
    order = {id(ch): i for i, ch in enumerate(doc.chunks)}
    return sorted(chunks, key=lambda ch: (phase_date(ch) is None, phase_date(ch) or datetime.min, order[id(ch)]))


def units_of(chunk) -> list:
    """The units drawn on a phase (saved with the project), as :class:`stratigraphy.UnitPolygon`."""
    from .stratigraphy import UnitPolygon

    out = []
    for d in ((chunk.meta or {}).get("drawings") or {}).get("units", []):
        try:
            out.append(UnitPolygon.from_dict(d))
        except Exception:  # a broken record: not a unit
            continue
    return out


def _name(unit) -> str:
    return f"{unit.tipo or 'US'} {unit.us}" if unit.us else (unit.tipo or "US") + " ?"


def _same(a, b) -> bool:
    return bool(a.us) and (a.us, a.area or "", a.sito or "", a.tipo or "US") == (b.us, b.area or "", b.sito or "",
                                                                                 b.tipo or "US")


def removed(doc, polygon=None, progress: Progress | None = None) -> list[dict]:
    """From each phase to the next (see the module docstring): ``{"from", "to"`` (chunk labels),
    ``"cut_m3", "fill_m3", "area_m2", "units": [{"name", "us", "area", "sito", "cut_m3", "fill_m3",
    "area_m2"}]}``."""
    from .terrain import volume

    progress = as_progress(progress)
    ph = phases(doc)
    out = []
    for i, (a, b) in enumerate(zip(ph, ph[1:])):
        progress.check()
        progress(i / max(len(ph) - 1, 1), f"Volumes: {a.label} -> {b.label}")
        try:
            v = volume(b.dem_path, reference=a.dem_path, polygon=polygon)
        except ValueError:  # the DEMs (or the polygon) do not overlap
            v = {"cut_m3": 0.0, "fill_m3": 0.0, "area_m2": 0.0}
        units = []
        for u in units_of(a):
            try:
                uv = volume(b.dem_path, reference=a.dem_path, polygon=u.geometry)
            except ValueError:
                uv = {"cut_m3": 0.0, "fill_m3": 0.0, "area_m2": 0.0}
            units.append({"name": _name(u), "us": u.us, "area": u.area, "sito": u.sito, "cut_m3": uv["cut_m3"],
                          "fill_m3": uv["fill_m3"], "area_m2": uv["area_m2"]})
        out.append({"from": a.label, "to": b.label, "from_key": a.key, "to_key": b.key, "cut_m3": v["cut_m3"],
                    "fill_m3": v["fill_m3"], "area_m2": v["area_m2"], "units": units})
    progress(1.0, "Volumes")
    return out


def suggested_relations(doc, min_overlap_m2: float = MIN_OVERLAP_M2,
                        min_share: float = MIN_OVERLAP_SHARE) -> list[dict]:
    """Units exposed in an earlier phase above the units they overlap in a later one:
    ``{"upper", "lower"`` (the units), ``"relation": "Copre", "inverse": "Coperto da",
    "overlap_m2", "phases": (earlier label, later label)}``, nearest phases first."""
    ph = phases(doc)
    units = [(i, ch, u) for i, ch in enumerate(ph) for u in units_of(ch)]
    out, seen = [], set()
    for i, ch_a, a in units:
        for j, ch_b, b in units:
            if j <= i or _same(a, b):
                continue
            overlap = a.geometry.intersection(b.geometry).area
            if overlap < max(min_overlap_m2, min_share * min(a.geometry.area, b.geometry.area)):
                continue
            key = (_name(a), a.area, _name(b), b.area)
            if key in seen:
                continue
            seen.add(key)
            out.append({"upper": a, "lower": b, "relation": "Copre", "inverse": "Coperto da",
                        "overlap_m2": float(overlap), "phases": (ch_a.label, ch_b.label), "gap": j - i})
    out.sort(key=lambda r: (r["gap"], r["phases"], _name(r["upper"])))
    return out


def timeline(doc, polygon=None, progress: Progress | None = None) -> dict:
    """Phases (label, date, DEM, units drawn), what was removed between them and the suggested
    relations."""
    ph = phases(doc)
    return {"phases": [{"label": ch.label, "key": ch.key, "date": phase_date(ch), "dem": str(ch.dem_path),
                        "units": [_name(u) for u in units_of(ch)]} for ch in ph],
            "removed": removed(doc, polygon=polygon, progress=progress),
            "relations": suggested_relations(doc)}


def timeline_csv(report: dict) -> str:
    """The report as CSV: one row per step between phases and per unit, then the relations."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["from", "to", "unit", "area", "cut_m3", "fill_m3", "area_m2"])
    for step in report["removed"]:
        w.writerow([step["from"], step["to"], "", "", f"{step['cut_m3']:.4f}", f"{step['fill_m3']:.4f}",
                    f"{step['area_m2']:.3f}"])
        for u in step["units"]:
            w.writerow([step["from"], step["to"], u["name"], u["area"] or "", f"{u['cut_m3']:.4f}",
                        f"{u['fill_m3']:.4f}", f"{u['area_m2']:.3f}"])
    w.writerow([])
    w.writerow(["upper", "upper_area", "relation", "lower", "lower_area", "overlap_m2", "phases"])
    for r in report["relations"]:
        w.writerow([_name(r["upper"]), r["upper"].area or "", r["relation"], _name(r["lower"]), r["lower"].area or "",
                    f"{r['overlap_m2']:.3f}", " -> ".join(r["phases"])])
    return buf.getvalue()


def write_relations(pa, relations, site: str | None = None) -> dict:
    """The suggested relations added to the US forms of PyArchInit (both ways, never twice):
    ``{"added": n, "missing": [units without a form]}``."""
    added, missing = 0, []
    for r in relations:
        for unit, rel, other in ((r["upper"], r["relation"], r["lower"]), (r["lower"], r["inverse"], r["upper"])):
            if not unit.us or not other.us:
                continue
            sito = unit.sito or site
            n = pa.add_relationships(sito, unit.area or "", unit.us,
                                     [[rel, other.us, other.area or "", other.sito or sito]], unit.tipo or "US")
            if n is None:
                missing.append(_name(unit))
            else:
                added += n
    return {"added": added, "missing": sorted(set(missing))}
