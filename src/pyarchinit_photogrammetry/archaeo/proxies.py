"""3D proxies of stratigraphic units.

A proxy is a closed solid standing for one unit (the "proxy" of the Extended Matrix): the
plan of the unit (its polygon) between a top and a bottom surface. The top is a DEM (the
surface documented when the unit was exposed); the bottom is a plane at a given
elevation, another DEM (the surface after the unit was removed), a constant thickness,
or the lowest point inside the polygon. The solid is built on the DEM grid (cells whose
centre falls inside the polygon), with shared corners, so it is watertight and its
volume equals the cut/fill volume of :func:`archaeo.terrain.volume` on the same cells.

Proxies are named like Extended Matrix nodes (``US12``, ``USM3``, ``USVs101``) and can be
exported as OBJ/PLY in world coordinates, published to PyArchInit as 3D media linked to
the unit, and used as solid IFC elements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class UnitProxy:
    name: str
    us: str | None
    area: str | None
    tipo: str
    mesh: object  # trimesh.Trimesh, world coordinates
    volume_m3: float
    area_m2: float
    z_top: tuple[float, float]  # min, max of the top surface
    z_bottom: tuple[float, float]
    attributes: dict = field(default_factory=dict)


def proxy_name(tipo: str | None, us: str | None) -> str:
    return f"{tipo or 'US'}{us}" if us else (tipo or "US")


def unit_proxy(dem_path: str | Path, polygon, base=None, thickness: float | None = None,
               top_dem: str | Path | None = None, us: str | None = None, area: str | None = None,
               tipo: str = "US", min_thickness: float = 0.002) -> UnitProxy:
    """Solid proxy of one unit. ``dem_path`` is the top surface (or ``top_dem`` when given);
    ``base``: None (lowest point), a float (plane elevation) or a DEM path (resampled);
    ``thickness`` instead of ``base``: constant depth below the top."""
    import trimesh

    from ..engine.dem import read_dem
    from .terrain import _coverage, _resample_to

    z, tr, _, _ = read_dem(Path(top_dem or dem_path))
    inside = (_coverage(polygon, z.shape, tr) >= 0.5) & np.isfinite(z)
    if not inside.any():
        raise ValueError("the unit polygon does not overlap the DEM")
    rows, cols = np.nonzero(inside)
    r0, r1, c0, c1 = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
    top = z[r0:r1, c0:c1].astype(float)
    mask = inside[r0:r1, c0:c1]
    if thickness is not None:
        bottom = top - float(thickness)
    elif base is None:
        bottom = np.full_like(top, np.nanmin(top[mask]))
    elif isinstance(base, (int, float)):
        bottom = np.full_like(top, float(base))
    else:
        full = _resample_to(Path(base), z.shape, tr)
        bottom = full[r0:r1, c0:c1]
        mask &= np.isfinite(bottom)
    hi = np.maximum(top, bottom)  # a unit removed later lies between the two surfaces
    lo = np.minimum(top, bottom)
    hi = np.where(hi - lo < min_thickness, lo + min_thickness, hi)
    h, w = mask.shape
    # corner grid: each corner takes the mean of the inside cells around it
    def corners(vals):
        acc = np.zeros((h + 1, w + 1))
        cnt = np.zeros((h + 1, w + 1))
        v = np.where(mask, vals, 0.0)
        m = mask.astype(float)
        for dr in (0, 1):
            for dc in (0, 1):
                acc[dr:dr + h, dc:dc + w] += v
                cnt[dr:dr + h, dc:dc + w] += m
        with np.errstate(invalid="ignore"):
            return acc / np.where(cnt > 0, cnt, np.nan), cnt > 0

    ztop, used = corners(hi)
    zbot, _ = corners(lo)
    idx_t = -np.ones((h + 1, w + 1), np.int64)
    idx_t[used] = np.arange(used.sum())
    n = int(used.sum())
    idx_b = np.where(used, idx_t + n, -1)
    rr, cc = np.nonzero(used)
    xs = tr.c + (c0 + cc) * tr.a
    ys = tr.f + (r0 + rr) * tr.e
    verts = np.vstack([np.column_stack([xs, ys, ztop[rr, cc]]), np.column_stack([xs, ys, zbot[rr, cc]])])
    faces = []
    for r, c in zip(*np.nonzero(mask)):
        a, b, cr, d = (r, c), (r, c + 1), (r + 1, c + 1), (r + 1, c)
        ta, tb, tc, td = idx_t[a], idx_t[b], idx_t[cr], idx_t[d]
        ba, bb, bc, bd = idx_b[a], idx_b[b], idx_b[cr], idx_b[d]
        faces += [(ta, td, tc), (ta, tc, tb), (ba, bb, bc), (ba, bc, bd)]
        for (nr, nc), (p, q) in (((r - 1, c), (a, b)), ((r, c + 1), (b, cr)), ((r + 1, c), (cr, d)), ((r, c - 1), (d, a))):
            if not (0 <= nr < h and 0 <= nc < w and mask[nr, nc]):
                faces += [(idx_t[p], idx_b[q], idx_b[p]), (idx_t[p], idx_t[q], idx_b[q])]
    # top faces are counter-clockwise seen from above for a north-up grid (e < 0), bottom
    # and wall faces follow, so the winding is consistent by construction; a south-up grid
    # turns everything inside out, which the volume sign reveals
    mesh = trimesh.Trimesh(verts, np.asarray(faces, np.int64), process=True)
    if mesh.volume < 0:
        mesh.invert()
    cell = abs(tr.a * tr.e)
    return UnitProxy(proxy_name(tipo, us), us, area, tipo, mesh, float(abs(mesh.volume)), float(mask.sum() * cell),
                     (float(np.nanmin(hi[mask])), float(np.nanmax(hi[mask]))),
                     (float(np.nanmin(lo[mask])), float(np.nanmax(lo[mask]))))


def proxies_for_units(dem_path, units, base=None, thickness=None, top_dem=None, progress=None) -> list[UnitProxy]:
    """One proxy per unit polygon (``UnitPolygon`` list); units without overlap are skipped."""
    out = []
    for k, u in enumerate(units):
        if progress:
            progress(k / max(len(units), 1), f"Proxy {proxy_name(u.tipo, u.us)}")
        geoms = getattr(u.geometry, "geoms", [u.geometry])
        parts = []
        for g in geoms:
            try:
                parts.append(unit_proxy(dem_path, g, base=base, thickness=thickness, top_dem=top_dem, us=u.us,
                                        area=u.area, tipo=u.tipo or "US"))
            except ValueError:
                continue
        if not parts:
            continue
        if len(parts) > 1:
            import trimesh

            p = parts[0]
            p.mesh = trimesh.util.concatenate([q.mesh for q in parts])
            p.volume_m3 = sum(q.volume_m3 for q in parts)
            p.area_m2 = sum(q.area_m2 for q in parts)
        proxy = parts[0]
        proxy.attributes = {k: v for k, v in (u.attributes or {}).items() if not str(k).startswith("_")}
        out.append(proxy)
    return out


def save_proxy(proxy: UnitProxy, folder: str | Path, fmt: str = "ply", offset=None) -> Path:
    """Write the proxy mesh (world coordinates; ``offset`` subtracted when given)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    mesh = proxy.mesh.copy()
    if offset is not None:
        mesh.vertices = np.asarray(mesh.vertices) - np.asarray(offset, float)
    path = folder / f"{proxy.name}.{fmt}"
    if fmt == "obj":
        mesh.metadata["name"] = proxy.name
        text = mesh.export(file_type="obj", include_normals=False)
        path.write_text(f"o {proxy.name}\n" + text, encoding="utf-8")
    else:
        mesh.export(str(path))
    return path


def export_extended_matrix(proxies: list[UnitProxy], path: str | Path, offset=None) -> Path:
    """One OBJ with one named object per unit (``o US12``...), as Extended Matrix tools
    expect proxies; ``offset`` (e.g. the integer part of the coordinates) keeps numbers small
    and is written in a comment and in ``<path>.offset.txt``."""
    path = Path(path)
    off = np.asarray(offset if offset is not None else np.zeros(3), float)
    lines = ["# pyarchinit-photogrammetry: stratigraphic unit proxies",
             f"# offset {off[0]:.3f} {off[1]:.3f} {off[2]:.3f}"]
    base = 1
    for p in proxies:
        v = np.asarray(p.mesh.vertices) - off
        lines.append(f"o {p.name}")
        lines += [f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in v]
        lines += [f"f {a + base} {b + base} {c + base}" for a, b, c in np.asarray(p.mesh.faces)]
        base += len(v)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(path.suffix + ".offset.txt").write_text(f"{off[0]:.3f} {off[1]:.3f} {off[2]:.3f}\n")
    return path


__all__ = ["UnitProxy", "unit_proxy", "proxies_for_units", "proxy_name", "save_proxy", "export_extended_matrix"]
