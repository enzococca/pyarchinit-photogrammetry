"""The drawings kept with an elevation follow the chunk: a scale from a known distance and an orientation of the local
system (engine.orient.apply) move the elevation's frame and its drawings together, and the 3D exports written
afterwards put every vertex where the same point of the survey is now (sub-millimetre)."""
from __future__ import annotations

import numpy as np
import pytest


def _internal(transform, xyz):
    from pyarchinit_photogrammetry.core import geometry as geo

    return geo.apply(np.linalg.inv(np.asarray(transform, float)), np.asarray(xyz, float).reshape(-1, 3))


@pytest.mark.parametrize("known", [2.5, None])
def test_exports_follow_a_scale_and_an_orientation(processed, tmp_path, known):
    import ezdxf
    from orient_helpers import centre, local_copy, motion, plane_elevation, rotation

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED
    from pyarchinit_photogrammetry.engine import orient

    _doc, ch, _ = local_copy(processed, tmp_path)
    entry = plane_elevation(ch, centre(ch))
    eid = entry["id"]
    before = ED.drawings(ch, eid, world=True)
    unit0 = np.asarray(before["units"][0]["world"].exterior.coords)
    quota0 = np.asarray(before["quote"][0]["world"].coords)
    t_old = np.asarray(ch.transform, float).copy()
    if known is not None:  # a scale from two points a known distance apart (ScaleStep's way), then a turn
        a, b = unit0[0], unit0[1]
        s = orient.scale_for(a, b, known)
        m = motion(rotation([0, 0, 1], 25.0), (1.0, -2.0, 0.5), s=s)
    else:  # an orientation with a tilt
        m = motion(rotation([1, 0.3, 0], 8.0), (0.2, 0.1, -0.4))
    orient.drop(orient.apply(ch, m)["state"])
    t_new = np.asarray(ch.transform, float)
    entry = ED.entry_of(ch, eid)
    after = ED.drawings(ch, eid, world=True)
    unit1 = np.asarray(after["units"][0]["world"].exterior.coords)
    # the same points of the survey: inside the chunk nothing moved
    assert np.abs(_internal(t_new, unit1) - _internal(t_old, unit0)).max() < 1e-6
    assert np.abs(_internal(t_new, np.asarray(after["quote"][0]["world"].coords))
                  - _internal(t_old, quota0)).max() < 1e-6
    if known is not None:
        assert np.linalg.norm(unit1[1] - unit1[0]) == pytest.approx(known, abs=1e-6)
    # the exports written now
    out = E.export_drawings(ch, entry, tmp_path / "out")
    msp = ezdxf.readfile(out["dxf_3d"]).modelspace()
    poly = next(e for e in msp if e.dxftype() == "POLYLINE" and e.dxf.layer == "US")
    pts = np.array([tuple(v.dxf.location) for v in poly.vertices])
    assert all(np.min(np.linalg.norm(unit1 - p, axis=1)) < 1e-4 for p in pts)  # 0.1 mm
    from pyarchinit_photogrammetry.io import gpkg

    (geom, _values), = gpkg.read_table(out["gpkg_world"], "unita")
    got = np.asarray(geom.geoms[0].exterior.coords)
    assert np.abs(_internal(t_new, got) - _internal(t_old, unit0)).max() < 1e-4
    # (u, v) of the unfolded GeoPackage: those of the drawing kept, and back on the frame the same world points
    (geom_uv, _v), = gpkg.read_table(out["gpkg"], "unita")
    uv = np.asarray(geom_uv.geoms[0].exterior.coords)
    assert np.abs(ED.world_of_uv(entry, uv, 0.0) - unit1).max() < 1e-4
