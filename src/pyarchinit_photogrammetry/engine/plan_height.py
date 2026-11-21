"""A chunk placed on another by a tie in plan and a height plane.

Two surveys of one site years apart (Khutm: a drone flight of 2017-18 with its GPS, a survey of 2024 on total-station
targets) often share few points that can be told apart in both - corners of houses, joints of a road - and wide
stretches of ground that did not change. The common points fix the plan well and the heights poorly (a point clicked on
an orthophoto takes its height from a DEM); the stable ground fixes the heights well, the tilt of a GPS block included,
and the plan not at all. :func:`measure` takes each from what fixes it:

1. **Plan**: the point ties between the two chunks (:mod:`.ties`, made by hand or imported), their ends in the world of
   each chunk: a 2D similarity (rotation about the vertical, scale, shift) bringing those of the chunk onto those of
   the reference, least squares. Each point gets its residual and its leave-one-out (where the similarity of the
   others puts it); the worst point is left out while its residual is over :data:`REJECT_K` times the median of the
   others (a point clicked on the wrong stone), then the similarity is fitted again. 3 points at least are kept.
2. **Heights**: the chunk moved in plan by that similarity (its heights scaled by its scale), its DEM against the
   reference's on stable ground - an area (a polygon layer of the free drawings, a file), else the stable-ground rules
   of the coregistration (gentle slopes, cells that agree) - and a plane fitted on the differences with Huber weights
   (:func:`..archaeo.coreg.height_offset`, as the stable heights of :mod:`.stable_height` with ``plane``).
3. **One transform**: the plan similarity and the height plane are one world -> world 4x4, an affine one - the plane
   shears the heights - which :func:`apply` puts before the chunk's transform (one step of *Annulla*, the report in
   ``chunk.meta["chunk_alignment"]``, method ``"plan_plane"``). The DEM and the orthophoto of the chunk are then built
   again.

Nothing is stored by :func:`measure`. No Qt here.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

from ..archaeo import coreg
from ..core.progress import Progress, as_progress
from . import stable_height, ties

if TYPE_CHECKING:  # pragma: no cover
    from ..core.document import Document

METHOD = "plan_plane"
REJECT_K = 3.0  # a common point further than this many medians of the others' residuals is left out
MIN_POINTS = 3


def similarity_2d(src: np.ndarray, dst: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """The 2D similarity ``dst ~ s R src + t`` (Umeyama, least squares): ``(s, R 2x2, t)``."""
    src, dst = np.asarray(src, float)[:, :2], np.asarray(dst, float)[:, :2]
    mu_s, mu_d = src.mean(0), dst.mean(0)
    a, b = src - mu_s, dst - mu_d
    u, sv, vt = np.linalg.svd(b.T @ a / len(src))
    d = np.eye(2)
    if np.linalg.det(u @ vt) < 0:
        d[1, 1] = -1.0
    rot = u @ d @ vt
    var = float(a.var(0).sum())
    s = float(np.trace(np.diag(sv) @ d) / var) if var > 0 else 1.0
    return s, rot, mu_d - s * rot @ mu_s


def _apply_2d(fit, xy: np.ndarray) -> np.ndarray:
    s, rot, t = fit
    return s * (np.asarray(xy, float)[:, :2] @ rot.T) + t


def plan_fit(names: list[str], src: np.ndarray, dst: np.ndarray, reject: float = REJECT_K) -> tuple[tuple, dict]:
    """Step 1 of the module: the similarity of the points ``src`` onto ``dst`` (plan), the worst point left out while
    its residual is over ``reject`` times the median of the others (:data:`MIN_POINTS` kept at least); every point's
    residual (under the final similarity) and leave-one-out (the similarity of the other points kept). Returns
    ``(fit, report)``."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    n = len(src)
    keep = np.ones(n, bool)
    span = float(np.ptp(dst[:, :2], axis=0).max()) if n else 0.0
    while keep.sum() > MIN_POINTS:
        fit = similarity_2d(src[keep], dst[keep])
        err = np.linalg.norm(_apply_2d(fit, src) - dst[:, :2], axis=1)
        idx = np.flatnonzero(keep)
        worst = idx[int(np.argmax(err[idx]))]
        others = err[idx[idx != worst]]
        if err[worst] <= reject * max(float(np.median(others)), 1e-6 * span):
            break
        keep[worst] = False
    fit = similarity_2d(src[keep], dst[keep])
    res = dst[:, :2] - _apply_2d(fit, src)
    rows = []
    for i in range(n):
        others = keep.copy()
        others[i] = False
        loo = float("nan")
        if others.sum() >= 2:
            loo = float(np.linalg.norm(dst[i, :2] - _apply_2d(similarity_2d(src[others], dst[others]), src[i:i + 1])[0]))
        rows.append({"name": names[i], "dx": float(res[i, 0]), "dy": float(res[i, 1]),
                     "residual": float(np.hypot(*res[i])), "loo": loo, "rejected": not bool(keep[i])})
    used = [r for r in rows if not r["rejected"]]
    s, rot, t = fit
    report = {"points": int(keep.sum()), "rejected": [r["name"] for r in rows if r["rejected"]], "scale": s,
              "rotation_deg": float(np.degrees(np.arctan2(rot[1, 0], rot[0, 0]))), "shift": [float(v) for v in t],
              "rms": float(np.sqrt(np.mean([r["residual"] ** 2 for r in used]))),
              "loo_max": float(np.nanmax([r["loo"] for r in used])) if used else float("nan"), "rows": rows}
    return fit, report


def plan_matrix(fit) -> np.ndarray:
    """The 4x4 of a plan similarity: x, y by it, heights scaled by its scale."""
    s, rot, t = fit
    m = np.eye(4)
    m[:2, :2] = s * rot
    m[:2, 3] = t
    m[2, 2] = s
    return m


def height_matrix(h: coreg.HeightOffset) -> np.ndarray:
    """The 4x4 that takes away the height plane ``h`` (the chunk above the reference by ``h.correction(x, y)``)."""
    m = np.eye(4)
    m[2, 0], m[2, 1] = -h.tilt_x, -h.tilt_y
    m[2, 3] = -h.dz + h.tilt_x * h.centre[0] + h.tilt_y * h.centre[1]
    return m


def _common_points(doc: "Document", ca, cb) -> tuple[list[str], np.ndarray, np.ndarray]:
    """The active point ties between ``ca`` (reference) and ``cb``: names, ends of ``cb`` and of ``ca`` in the world
    of the reference's system."""
    names, src, dst = [], [], []
    for t, group, pa, pb in ties._usable_ends(doc):
        if group[1] is None or set(group) != {ca.key, cb.key}:
            continue
        wa, wb = (pa, pb) if group[0] == ca.key else (pb, pa)
        wb = ties._to_system(np.asarray(wb, float)[None], cb.crs, ca.crs, cb, "the common points of a plan tie")[0]
        names.append(t.name)
        src.append(wb)
        dst.append(np.asarray(wa, float))
    return names, np.array(src, float).reshape(-1, 3), np.array(dst, float).reshape(-1, 3)


def measure(doc: "Document", chunk, reference, area=None, exclude=None, reject: float = REJECT_K,
            progress: Progress | None = None) -> dict:
    """Steps 1-3 of the module for ``chunk`` (B) on ``reference`` (A); nothing changed (:func:`apply`). ``area`` /
    ``exclude``: the stable ground (a polygon layer of the free drawings, a file, ``{"layer"}`` / ``{"file"}``: as
    :func:`.stable_height.measure`). Returns the report: ``plan`` (:func:`plan_fit`), ``height`` (the plane: ``dz`` at
    ``centre``, ``tilt_x``/``tilt_y`` in m/m, NMAD before and after, cells), ``transform`` (world -> world 4x4),
    ``new_transform`` (the chunk's after it) and ``placed`` (the transforms of now: :func:`apply` refuses when they
    changed). :class:`.ties.TieError` for fewer than three common points or systems that cannot be mixed;
    ``ValueError`` when a DEM is missing or the stable ground is too small."""
    progress = as_progress(progress)
    ca, cb = ties._chunk(doc, reference), ties._chunk(doc, chunk)
    if ca is cb:
        raise ValueError("a chunk cannot be placed on itself: choose another reference")
    if not ties._compatible(cb.crs, ca.crs):
        raise ties._not_same_frame(cb.label, cb.crs, ca.label, ca.crs)
    names, src, dst = _common_points(doc, ca, cb)
    if len(names) < MIN_POINTS:
        raise ties.TieError(f"{cb.label} -> {ca.label}: {len(names)} common points (point ties between the two chunks)"
                            f", at least {MIN_POINTS} common points are needed",
                            f"{cb.label} -> {ca.label}: {len(names)} punti comuni (legami fra i due chunk), ne servono "
                            f"almeno {MIN_POINTS}")
    progress(0.05, f"Plan of {cb.label} on {ca.label}: {len(names)} common points")
    fit, plan = plan_fit(names, src, dst, reject)
    P = plan_matrix(fit)
    before = np.asarray(cb.transform, float).copy()
    progress(0.2, f"Heights of {cb.label} on {ca.label}: the plane of the stable ground")
    try:  # B in A's plan for the measure of the heights, put back right after
        cb.transform = P @ before
        h = _height_plane(doc, ca, cb, area, exclude)
    finally:
        cb.transform = before
    M = height_matrix(h) @ P
    progress(1.0, f"{cb.label} on {ca.label}: plan RMS {plan['rms']:.4f}, heights NMAD {h.nmad_after:.4f}")
    return {"chunk": cb.key, "chunk_label": cb.label, "reference": ca.key, "reference_label": ca.label,
            "crs": ca.crs, "plan": plan,
            "height": {**h.to_dict(), "centre": [float(v) for v in h.centre], "cells": int(h.cells),
                       "compared": int(h.compared), "tilt_cm_per_100m": [float(h.tilt_x * 1e4), float(h.tilt_y * 1e4)],
                       "area": stable_height._area_spec(area), "exclude": stable_height._area_spec(exclude)},
            "transform": M.tolist(), "new_transform": (M @ before).tolist(),
            "placed": {"a": np.asarray(ca.transform, float).tolist(), "b": before.tolist(), "crs_b": cb.crs},
            "when": datetime.now().isoformat(timespec="seconds")}


def _height_plane(doc: "Document", ca, cb, area, exclude) -> coreg.HeightOffset:
    """The plane of B over A on the stable ground, the DEMs as the chunks are placed now (as
    :func:`.stable_height.measure` reads them)."""
    zr, tr_r = stable_height.dem_in_world(ca, ca)
    hr, wr = zr.shape
    margin = stable_height.BOUNDS_MARGIN * abs(tr_r.a)
    ra = (tr_r.c, tr_r.c + wr * tr_r.a, tr_r.f + hr * tr_r.e, tr_r.f)
    zd, tr_d = stable_height.dem_in_world(cb, ca, bounds=(ra[0] - margin, ra[2] - margin, ra[1] + margin,
                                                          ra[3] + margin))
    if not zd.size:
        raise ValueError(f"the DEMs of {cb.label} and {ca.label} do not overlap once placed in plan")
    system = ca.crs
    stable = stable_height._geometry(doc, stable_height._area_spec(area), [ca, cb], system, "stable")
    out = stable_height._geometry(doc, stable_height._area_spec(exclude), [ca, cb], system, "excluded")
    return coreg.height_offset(zr, tr_r, zd, tr_d, stable=stable, exclude=out, plane=True)


def apply(doc: "Document", result: dict) -> dict:
    """The transform of ``result`` (:func:`measure`) put before the chunk's: its new transform and the reference's
    system, ``chunk_alignment`` with the report, the free drawings carried. :class:`.ties.TieError` when either chunk
    moved since the measure. Returns what changed, for :func:`.ties.restore` (one step of *Annulla*), with
    ``stale_products``: the DEM and orthophoto built in the old transform."""
    from ..core import crs as crs_mod
    from ..core import free_drawings
    from .chunks import _convert_references

    ca, cb = ties._chunk(doc, result["reference"]), ties._chunk(doc, result["chunk"])
    placed = result["placed"]
    for ch, key in ((ca, "a"), (cb, "b")):
        if not np.allclose(np.asarray(ch.transform, float), np.asarray(placed[key], float), rtol=0.0, atol=1e-9):
            raise ties.TieError(f"chunk {ch.label} moved after the measure: measure again",
                                f"il chunk {ch.label} si è spostato dopo la misura: misura di nuovo")
    changed = {"chunks": {cb.key: {"transform": np.asarray(cb.transform, float).tolist(), "crs": cb.crs,
                                   "references": {m.key: copy.deepcopy(m.reference.location) for m in cb.markers},
                                   "chunk_alignment": copy.deepcopy(cb.meta.get("chunk_alignment")),
                                   "levelled": cb.meta.get("levelled"),
                                   "camera_reference_crs": {c.key: c.meta.get("reference_crs") for c in cb.cameras}}},
               "ties_report": copy.deepcopy(doc.meta.get("ties_report")), "stale_products": {}}
    if cb.crs != ca.crs and not crs_mod.is_local(cb.crs) and not crs_mod.is_local(ca.crs):
        _convert_references(cb, cb.crs, ca.crs)
    cb.transform = np.asarray(result["new_transform"], float).copy()
    cb.crs = ca.crs
    plan, height = result["plan"], result["height"]
    cb.meta["chunk_alignment"] = {"method": METHOD, "reference": ca.label, "reference_key": ca.key,
                                  "plan_rms": plan["rms"], "plan_loo_max": plan["loo_max"], "points": plan["points"],
                                  "rejected": list(plan["rejected"]), "height_nmad": height.get("nmad_after"),
                                  "height_cells": height.get("cells"), "transform": result["transform"],
                                  "when": result.get("when")}
    if ca.meta.get("levelled"):
        cb.meta["levelled"] = True
    free_drawings.follow(cb)
    stale = [k for k in ("dem", "orthomosaic") if cb.products.get(k)]
    if stale:
        changed["stale_products"][cb.key] = stale
    return changed


def report_text(result: dict) -> str:
    """The report of :func:`measure` as lines of text (English: the CLI)."""
    plan, h = result["plan"], result["height"]
    lines = [f"{result['chunk_label']} -> {result['reference_label']}: plan from {plan['points']} common points: "
             f"scale {plan['scale']:.6f} ({(plan['scale'] - 1) * 1e6:+.0f} ppm), rotation {plan['rotation_deg']:+.4f} "
             f"deg, RMS {plan['rms']:.4f}, leave-one-out max {plan['loo_max']:.4f}"]
    for r in plan["rows"]:
        lines.append(f"  {r['name']}: residual {r['residual']:.4f}, leave-one-out {r['loo']:.4f}"
                     + (" - left out" if r["rejected"] else ""))
    lines.append(f"  heights: plane on {h['cells']} stable cells, dz {h['dz']:+.4f} at the middle, tilt "
                 f"{h['tilt_cm_per_100m'][0]:+.1f} / {h['tilt_cm_per_100m'][1]:+.1f} cm per 100 m (east / north), "
                 f"NMAD {h['nmad_before']:.4f} before, {h['nmad_after']:.4f} after")
    return "\n".join(lines)


__all__ = ["measure", "apply", "plan_fit", "similarity_2d", "plan_matrix", "height_matrix", "report_text", "METHOD",
           "REJECT_K"]
