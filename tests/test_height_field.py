"""Height-field surface: the edge limit is local, so that a cloud of uneven density has no
holes where it is sparser; empty areas and the outside margin stay open, and holes are closed
only on request (and reported as interpolated)."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import meshing

UP = np.array([0.0, 0.0, 1.0])
HOLE = np.array([6.5, 5.0])  # centre of the area without points
HOLE_RADIUS = 1.5


def uneven_cloud(notch: bool = False) -> np.ndarray:
    """10 x 10 m of ground: 2500 points per square metre in a strip of 3 m, 25 in the rest
    (a spacing of 2 cm against 20 cm), nothing in a disc of 3 m; ``notch``: nothing in the
    corner of 2 x 2 m either, a re-entrant outline."""
    rng = np.random.default_rng(3)
    dense = rng.uniform([0, 0], [3, 10], (75_000, 2))
    sparse = rng.uniform([3, 0], [10, 10], (1750, 2))
    xy = np.vstack([dense, sparse])
    xy = xy[np.linalg.norm(xy - HOLE, axis=1) > HOLE_RADIUS]
    if notch:
        xy = xy[~((xy[:, 0] > 8) & (xy[:, 1] > 8))]
    z = 0.2 * np.sin(xy[:, 0]) * np.cos(0.7 * xy[:, 1])
    return np.column_stack([xy, z])


def plan_area(mesh, lo=(0.0, 0.0), hi=(10.0, 10.0)) -> float:
    """Area in plan of the faces whose centre is in the rectangle."""
    v = mesh.vertices[:, :2][mesh.faces]
    c = v.mean(axis=1)
    keep = (c >= lo).all(axis=1) & (c <= hi).all(axis=1)
    a, b = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
    return float(0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])[keep].sum())


def covered(mesh, p) -> bool:
    """Whether a face lies over the point of the plan."""
    v = mesh.vertices[:, :2][mesh.faces] - np.asarray(p, float)
    cross = [v[:, i, 0] * v[:, (i + 1) % 3, 1] - v[:, i, 1] * v[:, (i + 1) % 3, 0] for i in range(3)]
    return bool(((np.sign(cross[0]) == np.sign(cross[1])) & (np.sign(cross[1]) == np.sign(cross[2]))).any())


def test_sparser_zone_has_no_holes():
    """With one limit for the whole cloud (set by the dense strip) the sparse zone was riddled
    with holes: 7 x 10 m less the disc must be covered."""
    mesh = meshing._height_field(uneven_cloud(), UP)
    expected = 70.0 - np.pi * HOLE_RADIUS ** 2
    assert plan_area(mesh, lo=(3.0, 0.0)) > 0.93 * expected
    assert plan_area(mesh, hi=(3.0, 10.0)) > 0.97 * 30.0
    for p in ((5.0, 1.0), (9.0, 9.0), (4.0, 8.0), (8.5, 2.0), (3.05, 5.0)):
        assert covered(mesh, p)
    assert (mesh.face_normals @ UP > 0).all()


def test_empty_area_stays_open():
    """Nothing was measured in the disc: no face over its middle, and little over the rest."""
    mesh = meshing._height_field(uneven_cloud(), UP)
    assert not covered(mesh, HOLE)
    assert not covered(mesh, HOLE + [0.5, 0.0]) and not covered(mesh, HOLE - [0.0, 0.5])
    assert plan_area(mesh) < 100.0 - 0.5 * np.pi * HOLE_RADIUS ** 2


def test_holes_closed_up_to_a_size():
    report: dict = {}
    small = meshing._height_field(uneven_cloud(), UP, fill_holes=1.0, report=report)
    assert not covered(small, HOLE)  # a gap of 3 m is larger than 1 m
    assert report["holes_left"] >= 1
    report = {}
    mesh = meshing._height_field(uneven_cloud(), UP, fill_holes=3.5, report=report)
    assert covered(mesh, HOLE)
    assert plan_area(mesh) > 99.0
    # the faces that close the holes are counted apart: the disc, less what the local limit spans
    assert report["holes_filled"] >= 1
    assert 0.4 * np.pi * HOLE_RADIUS ** 2 < report["interpolated_area"] < 1.1 * np.pi * HOLE_RADIUS ** 2
    # the heights over the hole are those of its rim (interpolated on flat faces)
    assert np.abs(mesh.vertices[:, 2]).max() <= 0.2 + 1e-9


def test_outside_margin_is_never_closed():
    """A re-entrant corner is outside the survey, however large the holes to close."""
    report: dict = {}
    mesh = meshing._height_field(uneven_cloud(notch=True), UP, fill_holes=100.0, report=report)
    assert covered(mesh, HOLE)
    for p in ((9.4, 9.4), (9.8, 9.0), (9.0, 9.8)):
        assert not covered(mesh, p)
    assert plan_area(mesh, lo=(8.9, 8.9)) < 0.3
    assert report["holes_left"] == 0


@pytest.fixture
def chunk(tmp_path):
    doc = Document()
    ch = doc.add_chunk("terreno")
    doc.save(tmp_path / "t.ppg")
    xyz = uneven_cloud()
    ch.point_cloud = PointCloud(xyz, np.full((len(xyz), 3), 120, np.uint8))
    return ch


def test_build_model_reports_what_is_interpolated(chunk):
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9)
    report = chunk.meta["model"]
    assert report["surface_used"] == "height_field" and report["fill_holes"] is None
    assert report["interpolated_area"] == 0.0 and not covered(mesh, HOLE)
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=3.5)
    report = chunk.meta["model"]
    assert covered(mesh, HOLE)
    assert report["fill_holes"] == 3.5 and report["holes_filled"] >= 1
    assert 2.5 < report["interpolated_area"] < 8.0  # square metres, in plan


def test_fill_holes_is_in_metres(chunk):
    """A chunk scaled by 2: a hole of 3 internal units is 6 m wide."""
    chunk.transform = np.diag([2.0, 2.0, 2.0, 1.0])
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=3.5)
    assert not covered(mesh, HOLE)
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=7.0)
    assert covered(mesh, HOLE)
    assert 10.0 < chunk.meta["model"]["interpolated_area"] < 32.0


def test_height_field_is_mapped_from_above(chunk):
    """A height field shows every face from above: the orthophoto mapping, whatever its size."""
    from pyarchinit_photogrammetry.engine.texturing import choose_mapping

    mesh = chunk.build_model(surface="height_field", face_count=5000)
    assert len(mesh.faces) < 200_000
    assert choose_mapping(chunk, mesh.vertices, mesh.faces) == "orthophoto"
    chunk.meta["model"]["surface"] = "arbitrary"  # also when Poisson fell back to the height field
    assert choose_mapping(chunk, mesh.vertices, mesh.faces) == "generic"


def test_model_dialog_closes_the_holes(trench, monkeypatch):
    """*Flusso di lavoro > Costruisci modello*: the gap to close is a field of the dialog, and the log says how
    much of the surface is interpolated."""
    w, app, mw = trench
    xyz = uneven_cloud()
    w.chunk.point_cloud = PointCloud(xyz, np.full((len(xyz), 3), 120, np.uint8))
    asked, said = [], []

    def ask(_parent, title, fields, intro=""):
        asked.append({f.name: f.default for f in fields})
        return {**asked[-1], "source": "point_cloud", "surface": "height_field", "face_count": "high",
                "fill_holes": 3.5}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    monkeypatch.setattr(w, "_need_chunk", lambda *a, **k: True)
    monkeypatch.setattr(w.log_pane, "warning", lambda text, *a: said.append(text))
    w.build_model()
    w.runner.wait()
    app.processEvents()
    assert asked[0]["fill_holes"] == 0.0  # nothing is closed unless asked
    report = w.chunk.meta["model"]
    assert report["fill_holes"] == 3.5 and report["holes_filled"] >= 1 and report["interpolated_area"] > 2.5
    assert said and "interpolati (non misurati)" in said[-1]
