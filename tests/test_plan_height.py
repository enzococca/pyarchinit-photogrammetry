"""A tie in plan and a height plane (engine/plan_height.py): the plan from the common points of two chunks (a 2D
similarity, leave-one-out, the outliers left out), the heights from a plane fitted on the difference of their DEMs on
stable ground, applied as one 4x4 transform. Khutm 2026-11-20: the 2017-18 drone flight placed on the 2024 survey by
seven common points and the stable ground around the tower.

The surveys are those of ``test_auto_ties``: A placed right, B placed by a wrong plan similarity (1.5 degrees, +2000
ppm, 0.15 / -0.10 m) whose heights are also tilted (2 cm per metre east, -1 cm per metre north) and moved 0.3 m up:
what a GPS block can be (a tilt that a plan similarity with one height offset cannot take away)."""
from __future__ import annotations

import numpy as np
import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import plan_height, ties

from test_auto_ties import _errors, _rot_z, _survey, _translate  # noqa: E402

TILT = (0.02, -0.01)  # metres per metre, east and north


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.004)


def _surveys(tmp_path, scene, outlier: bool = True):
    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    origin = _translate(scene.origin)
    truth_b = origin @ _rot_z(25.0, 1.0, (0.4, -0.2, 0.0))
    centre = np.array(scene.origin) + (2.7, 1.5, 0.0)
    wrong = _rot_z(1.5, 1.002, (0.15, -0.10, 0.30))
    wrong[2, 0], wrong[2, 1] = TILT
    error = _translate(centre) @ wrong @ _translate(-centre)
    a = _survey(doc, scene, "A", (0.0, 2.6), origin, seed=1)
    b = _survey(doc, scene, "B", (1.4, 4.0), truth_b, error, seed=2)
    # the common points: true places in the overlap, each end where its chunk shows it, 1 mm of noise in plan
    rng = np.random.default_rng(4)
    x0, y0, z0 = scene.origin
    places = [(1.5, 0.3), (2.5, 0.4), (1.6, 2.7), (2.5, 2.6), (2.0, 1.5), (1.8, 0.9), (2.3, 2.0)]
    for i, (x, y) in enumerate(places):
        p = np.array([x0 + x, y0 + y, z0 + float(scene.height_at(np.array([x]), np.array([y]))[0])])
        pa = p + np.r_[rng.normal(0, 0.001, 2), 0.0]
        pb = p + (np.array([0.8, -0.6, 0.0]) if outlier and i == 4 else 0.0)  # P5 clicked on the wrong stone
        ia = a.to_internal(pa[None])[0]
        ib = (np.linalg.inv(truth_b) @ np.r_[pb, 1.0])[:3]
        ties.add_tie(doc, a, ia, b, ib, name=f"P{i + 1}", precision=0.01)
    doc.save()
    return doc, a, b, truth_b


def test_plan_from_points_and_heights_from_a_plane(tmp_path, scene):
    """The plan similarity from the common points (the point clicked on the wrong stone left out and named, every
    point with its leave-one-out), the heights from the plane of the DEM difference, one 4x4 that brings B back to
    where it truly is: the tilt the GPS left included."""
    doc, a, b, truth_b = _surveys(tmp_path, scene)
    before = np.asarray(b.transform, float).copy()
    r = plan_height.measure(doc, b, a)
    plan = r["plan"]
    assert plan["rejected"] == ["P5"] and plan["points"] == 6
    rows = {p["name"]: p for p in plan["rows"]}
    assert rows["P5"]["rejected"] and rows["P5"]["residual"] > 0.5
    assert plan["rms"] < 0.003 and plan["loo_max"] < 0.005
    assert abs(plan["rotation_deg"] + 1.5) < 0.1 and abs(plan["scale"] * 1.002 - 1.0) < 2e-3
    height = r["height"]
    assert height["cells"] > 1000 and height["nmad_after"] < 0.005
    assert np.allclose(np.asarray(b.transform, float), before)  # nothing changed before apply
    changed = plan_height.apply(doc, r)
    _shift, rot, ppm, worst = _errors(doc, b, truth_b, scene.origin)
    print(f"plan + plane: rotation {rot:.4f} deg, scale {ppm:+.0f} ppm, worst corner {worst * 1000:.2f} mm")
    assert worst < 0.01 and rot < 0.1
    corners = np.array([[1.4, 0.0], [4.0, 0.0], [1.4, 3.0], [4.0, 3.0]]) + np.asarray(scene.origin)[:2]
    pts = np.column_stack([corners, np.full(4, scene.origin[2])])
    internal = (np.linalg.inv(truth_b) @ np.column_stack([pts, np.ones(4)]).T).T[:, :3]
    dz = b.to_world(internal)[:, 2] - pts[:, 2]
    assert np.abs(dz).max() < 0.01  # the heights too, at the corners of B: the tilt taken away
    alignment = b.meta["chunk_alignment"]
    assert alignment["method"] == "plan_plane" and alignment["reference"] == "A"
    assert changed["stale_products"][b.key] == ["dem", "orthomosaic"]
    ties.restore(doc, changed)  # one Annulla step
    assert np.allclose(np.asarray(b.transform, float), before)


def _to_other_system(b, crs: str) -> None:
    """Chunk ``b`` carried into another projected system as a chunk surveyed there would be: its transform through
    the projection (as the solve of the ties carries it), its DEM reprojected and stamped with the new transform."""
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine.dem import NODATA

    T, _err = ties.world_in(b, crs)
    src_path = b.dem_path
    with rasterio.open(src_path) as src:
        z = src.read(1)
        tr, w, h = calculate_default_transform(src.crs, crs, src.width, src.height, *src.bounds)
        out = np.full((h, w), NODATA, np.float32)
        reproject(z, out, src_transform=src.transform, src_crs=src.crs, src_nodata=NODATA, dst_transform=tr,
                  dst_crs=crs, dst_nodata=NODATA, resampling=Resampling.bilinear)
    path = b.data_dir / "dem_other.tif"
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=crs, transform=tr,
                       nodata=NODATA) as dst:
        dst.write(out, 1)
    b.transform = T
    b.crs = crs
    b._set_product("dem", path)
    stamps.stamp(b, "dem")


def test_a_chunk_in_another_projected_system(tmp_path, scene):
    """B surveyed in another projected system (Gauss-Boaga west against east: another false easting and a
    convergence of the meridians of degrees): the plan is fitted in the system of A, the DEM of B read there, and B
    ends in A's system where it truly is. A reference in a geographic system is refused, as by the solve."""
    doc, a, b, truth_b = _surveys(tmp_path, scene)
    _to_other_system(b, "EPSG:3003")
    r = plan_height.measure(doc, b, a)
    assert r["plan"]["rejected"] == ["P5"] and r["height"]["nmad_after"] < 0.005
    assert 0.0 < r["system_fit_rms"] < 0.02
    plan_height.apply(doc, r)
    assert b.crs == a.crs
    _shift, rot, _ppm, worst = _errors(doc, b, truth_b, scene.origin)
    # a similarity through the projection (as the solve carries a chunk) cannot scale x, y and not z: here 900 ppm
    # (6 degrees from the meridian of the west zone) over the heights of the box, twice - B was carried there the same
    # way. Before the fix B landed hundreds of metres off
    assert worst < 0.03 and rot < 0.1
    a.crs = "EPSG:4326"
    with pytest.raises(ties.TieError, match="geographic"):
        plan_height.measure(doc, b, a)


def test_the_measure_never_moves_the_chunk(tmp_path, scene, monkeypatch):
    """The measure runs in the worker: the chunk it places is never changed while it runs (no transform written to
    it, not even for a moment)."""
    doc, a, b, _truth = _surveys(tmp_path, scene)
    seen = []
    cls = type(b)
    original = np.asarray(b.transform, float).copy()

    def watch(obj, name, value):
        if name == "transform" and obj is b:
            seen.append(np.asarray(value, float).copy())
        object.__setattr__(obj, name, value)

    monkeypatch.setattr(cls, "__setattr__", watch)
    plan_height.measure(doc, b, a)
    assert not seen and np.allclose(np.asarray(b.transform, float), original)


def test_too_few_points_or_moved_chunks_are_refused(tmp_path, scene):
    doc, a, b, _truth = _surveys(tmp_path, scene, outlier=False)
    for t in ties.ties_of(doc)[2:]:
        ties.remove_tie(doc, t.id)
    with pytest.raises(ties.TieError, match="3 common points"):
        plan_height.measure(doc, b, a)
    doc, a, b, _truth = _surveys(tmp_path / "moved", scene, outlier=False)
    r = plan_height.measure(doc, b, a)
    b.transform = _translate((0.01, 0.0, 0.0)) @ b.transform
    with pytest.raises(ties.TieError, match="moved"):
        plan_height.apply(doc, r)


def test_cli_plan_height(tmp_path, scene):
    _surveys(tmp_path, scene)
    run = CliRunner().invoke
    out = run(app, ["ties", "plan-height", str(tmp_path / "p.ppg"), "B", "A", "--json", str(tmp_path / "r.json")])
    assert out.exit_code == 0, out.output
    assert "P5" in out.output and "left out" in out.output and "not applied" in out.output
    assert (tmp_path / "r.json").exists()
    saved = ppg.Document.open(tmp_path / "p.ppg")
    assert "chunk_alignment" not in saved.chunks[1].meta
    out = run(app, ["ties", "plan-height", str(tmp_path / "p.ppg"), "B", "A", "--apply"])
    assert out.exit_code == 0, out.output
    assert "applied" in out.output
    saved = ppg.Document.open(tmp_path / "p.ppg")
    assert saved.chunks[1].meta["chunk_alignment"]["method"] == "plan_plane"
