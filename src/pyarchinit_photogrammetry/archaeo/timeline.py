"""Excavation over time: the phases of an excavation are the chunks of a project, one per
survey (a day, the end of a US), in one frame (a common georeference, or *Allinea chunk*).

* :func:`phase_date` - the date of a phase: ``chunk.meta["date"]`` when set (:func:`set_phase_date`),
  else the earliest date its photos were taken (EXIF).
* :func:`phases` - the chunks with a DEM, in date order (undated ones last, in project order).
* :func:`removed` - from each phase to the next: the volume removed and added where both DEMs
  are (or inside a polygon), and for every unit drawn on the earlier phase - exposed then, and dug
  away before the next survey - the volume removed inside it.
* :func:`coregister_phases` - surveys of different days are never perfectly registered, and a
  shift of a few centimetres between two DEMs is false excavation and fill on every slope: every
  phase is coregistered (:mod:`.coreg`) onto the first, or onto the one before it, over the ground
  that did not change (the units drawn on the phases in between left out). :func:`removed` and
  :func:`timeline` do it first with ``coregister=True``; the DEMs of the project stay as they are.
* :func:`suggested_relations` - a unit exposed in an earlier phase that overlaps in plan a unit
  exposed in a later one lay above it: ``Copre`` / ``Coperto da`` in PyArchInit's terms, as
  suggestions for the US forms.
* :func:`timeline` - all of it, for the interface, the command line and :func:`timeline_csv`.
"""
from __future__ import annotations

import csv
import io
import tempfile
from datetime import datetime
from pathlib import Path

from ..core.progress import Progress, as_progress

MIN_OVERLAP_M2 = 0.01  # touching outlines are not a relation
MIN_OVERLAP_SHARE = 0.05  # of the smaller unit
COREG_REFERENCES = ("first", "previous")  # every phase coregistered onto the first one, or onto the one before


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


def coregister_phases(doc, folder, reference: str = "first", tilt: bool = True,
                      progress: Progress | None = None) -> tuple[dict, dict]:
    """Every phase after the first coregistered (:func:`.coreg.coregister`) onto the first phase
    (``reference="first"``) or onto the phase before it, itself coregistered already
    (``"previous"``): all of them end in the frame of the first. The stable ground leaves out the
    units drawn on the phases from the reference to the phase (they were dug or exposed in between),
    unless that leaves too little ground; the cells that changed are found anyway. The DEMs are
    written in ``folder``, the project is not touched. Returns ``({chunk key: DEM}, {chunk key:
    measures})``: the first phase keeps its DEM (measures None); a phase that cannot be coregistered
    keeps its DEM too, with ``"error"`` in its measures."""
    from . import coreg

    if reference not in COREG_REFERENCES:
        raise ValueError(f"reference: one of {', '.join(COREG_REFERENCES)}")
    progress = as_progress(progress)
    ph = phases(doc)
    paths = {ch.key: ch.dem_path for ch in ph}
    info: dict = {ph[0].key: None} if ph else {}
    for i, ch in enumerate(ph[1:], start=1):
        progress.check()
        first = 0 if reference == "first" else i - 1
        ref = ph[first]
        sub = progress.sub((i - 1) / (len(ph) - 1), 1 / (len(ph) - 1))
        sub(0.0, f"Coregistration: {ch.label} -> {ref.label}")
        units = [u.geometry for c in ph[first:i + 1] for u in units_of(c)
                 if u.geometry is not None and not u.geometry.is_empty]
        out = Path(folder) / f"phase_{ch.key}.tif"
        result, excluded, error = None, False, None
        for exclude in ([units, None] if units else [None]):
            try:
                result = coreg.coregister(paths[ref.key], ch.dem_path, out, exclude=exclude, tilt=tilt, progress=sub)
                excluded = exclude is not None
                break
            except ValueError as exc:  # too little stable ground (with the units left out), or no overlap
                error = str(exc)
        if result is None:
            info[ch.key] = {"reference": ref.label, "reference_key": ref.key, "error": error}
            continue
        paths[ch.key] = out
        rec = {k: v for k, v in result.to_dict().items() if k not in ("reference", "dem", "path")}
        info[ch.key] = {**rec, "reference": ref.label, "reference_key": ref.key, "units_excluded": excluded}
    progress(1.0, "Coregistration of the phases")
    return paths, info


def removed(doc, polygon=None, progress: Progress | None = None, coregister: bool = False,
            reference: str = "first", tilt: bool = True) -> list[dict]:
    """From each phase to the next (see the module docstring): ``{"from", "to"`` (chunk labels),
    ``"cut_m3", "fill_m3", "area_m2", "units": [{"name", "us", "area", "sito", "cut_m3", "fill_m3",
    "area_m2"}], "coregistration"}``. With ``coregister`` the DEMs are coregistered first
    (:func:`coregister_phases`, ``reference`` and ``tilt``); ``"coregistration"`` gives the measures
    of the later phase of the step (None without)."""
    progress = as_progress(progress)
    ph = phases(doc)
    if coregister and len(ph) > 1:
        with tempfile.TemporaryDirectory(prefix="coreg-", dir=doc.files_dir) as tmp:
            paths, info = coregister_phases(doc, tmp, reference, tilt, progress.sub(0.0, 0.6))
            return _volumes(ph, paths, info, polygon, progress.sub(0.6, 0.4))
    return _volumes(ph, {ch.key: ch.dem_path for ch in ph}, {}, polygon, progress)


def _volumes(ph, paths: dict, info: dict, polygon, progress: Progress) -> list[dict]:
    from .terrain import volume

    out = []
    for i, (a, b) in enumerate(zip(ph, ph[1:])):
        progress.check()
        progress(i / max(len(ph) - 1, 1), f"Volumes: {a.label} -> {b.label}")
        before, after = paths[a.key], paths[b.key]
        try:
            v = volume(after, reference=before, polygon=polygon)
        except ValueError:  # the DEMs (or the polygon) do not overlap
            v = {"cut_m3": 0.0, "fill_m3": 0.0, "area_m2": 0.0}
        units = []
        for u in units_of(a):
            try:
                uv = volume(after, reference=before, polygon=u.geometry)
            except ValueError:
                uv = {"cut_m3": 0.0, "fill_m3": 0.0, "area_m2": 0.0}
            units.append({"name": _name(u), "us": u.us, "area": u.area, "sito": u.sito, "cut_m3": uv["cut_m3"],
                          "fill_m3": uv["fill_m3"], "area_m2": uv["area_m2"]})
        out.append({"from": a.label, "to": b.label, "from_key": a.key, "to_key": b.key, "cut_m3": v["cut_m3"],
                    "fill_m3": v["fill_m3"], "area_m2": v["area_m2"], "units": units,
                    "coregistration": info.get(b.key)})
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


def timeline(doc, polygon=None, progress: Progress | None = None, coregister: bool = False,
             reference: str = "first", tilt: bool = True) -> dict:
    """Phases (label, date, DEM, units drawn, ``"coregistration"``: the measures of
    :func:`coregister_phases` with ``coregister``), what was removed between them and the suggested
    relations; ``"coregistration"``: ``{"reference", "tilt"}`` when the phases were coregistered."""
    ph = phases(doc)
    steps = removed(doc, polygon=polygon, progress=progress, coregister=coregister, reference=reference, tilt=tilt)
    measures = {s["to_key"]: s.get("coregistration") for s in steps}
    done = coregister and len(ph) > 1
    return {"phases": [{"label": ch.label, "key": ch.key, "date": phase_date(ch), "dem": str(ch.dem_path),
                        "units": [_name(u) for u in units_of(ch)], "coregistration": measures.get(ch.key)}
                       for ch in ph],
            "removed": steps,
            "relations": suggested_relations(doc),
            "coregistration": {"reference": reference, "tilt": tilt} if done else None}


def coregistration_text(c: dict | None) -> str:
    """One line on the coregistration of a phase (the command line, the CSV)."""
    if not c:
        return ""
    if c.get("error"):
        return f"not coregistered on {c['reference']}: {c['error']}"
    from .coreg import summary

    return f"coregistered on {c['reference']}: {summary(c)}"


def timeline_csv(report: dict) -> str:
    """The report as CSV: one row per step between phases and per unit, then the relations and,
    when the phases were coregistered, the shift of each one."""
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
    rows = [(p["label"], p["coregistration"]) for p in report["phases"] if p.get("coregistration")]
    if rows:
        def num(v, fmt):
            return "" if v is None else format(v, fmt)

        w.writerow([])
        w.writerow(["phase", "coregistered_on", "dx_m", "dy_m", "dz_m", "tilt_x_m_per_m", "tilt_y_m_per_m",
                    "nmad_before_m", "nmad_after_m", "stable_cells", "note"])
        for label, c in rows:
            if c.get("error"):
                w.writerow([label, c["reference"], "", "", "", "", "", "", "", "", c["error"]])
                continue
            w.writerow([label, c["reference"], num(c["dx"], ".4f"), num(c["dy"], ".4f"), num(c["dz"], ".4f"),
                        num(c["tilt_x"], ".6f"), num(c["tilt_y"], ".6f"), num(c["nmad_before"], ".4f"),
                        num(c["nmad_after"], ".4f"), c["cells"],
                        "" if c.get("horizontal", True) else "horizontal shift not measurable"])
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
