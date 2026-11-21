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
