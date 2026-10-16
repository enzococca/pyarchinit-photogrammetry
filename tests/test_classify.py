"""Points classified by hand: stored in the PLY, kept by the automatic classification."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.ai import classify
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.testing.synthetic import make_classified_cloud


def test_manual_flag_in_ply(tmp_path):
    rng = np.random.default_rng(0)
    xyz = rng.random((50, 3))
    old = PointCloud(xyz, classification=np.full(50, 2))
    old.save_ply(tmp_path / "old.ply")
    assert b"manual" not in (tmp_path / "old.ply").read_bytes()[:400]  # same file as before
    back = PointCloud.load(tmp_path / "old.ply")
    assert back.manual is None and (back.classification == 2).all()
    manual = rng.random(50) < 0.3
    pc = PointCloud(xyz, rgb=rng.integers(0, 255, (50, 3)), classification=np.full(50, 2), manual=manual)
    pc.save_ply(tmp_path / "new.ply")
    back = PointCloud.load(tmp_path / "new.ply")
    assert np.array_equal(back.manual, manual) and back.manual.dtype == bool
    assert np.array_equal(back.subset(manual).manual, manual[manual])
    assert np.array_equal(pc.transformed(np.diag([2.0, 2.0, 2.0, 1.0])).manual, manual)


def test_rule_based_classification_keeps_hand_labels(tmp_path):
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = make_classified_cloud(spacing=0.04)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:3004"
    ch.transform = geometry.compose_similarity(0.5, np.eye(3), xyz.mean(0))
    doc.save(tmp_path / "scavo.ppg")
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb)
    wall = np.nonzero(codes == 64)[0][:30]
    ground = np.nonzero(codes == 2)[0][:20]
    assert classify.set_class(ch, wall, 70, label="Roccia") == 30  # indices
    mask = np.zeros(len(xyz), bool)
    mask[ground] = True
    assert classify.set_class(ch, mask, 2) == 20  # boolean mask
    with pytest.raises(ValueError):
        classify.set_class(ch, mask[:-1], 2)
    pc = ch.point_cloud
    assert pc.manual.sum() == 50 and (pc.classification[wall] == 70).all()
    assert ch.meta["classes"]["70"] == "Roccia" and classify.class_names(ch)[70] == "Roccia"
    ruled = classify.classify_chunk(ch)
    assert (ruled.classification[wall] == 70).all() and ruled.manual.sum() == 50
    assert (ruled.classification[codes == 64] == classify.MASONRY).mean() > 0.5  # the others are classified
    ruled = classify.classify_chunk(ch, overwrite_manual=True)
    assert (ruled.classification[wall] != 70).all() and not ruled.manual[wall].any()
    assert ruled.manual[ground].sum() >= 15  # still flagged where the rules agree
    classify.set_class(ch, ground[:5], 3, manual=False)
    pc = ch.point_cloud
    assert (pc.classification[ground[:5]] == 3).all() and not pc.manual[ground[:5]].any()
    assert classify.label_points(pc, wall[:4], 64) == 4 and pc.manual[wall[:4]].all()  # in memory only
    assert not ch.point_cloud.manual[wall[:4]].any()
    ch.point_cloud = pc
    remaining = int(pc.manual.sum())
    assert classify.clear_manual(ch) == remaining and not ch.point_cloud.manual.any()


# ---------------------------------------------------------------- products from chosen classes
@pytest.fixture
def excavation(tmp_path):
    """The synthetic excavation (ground 2, wall 64, bushes 3, noise 7) as a georeferenced chunk."""
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = make_classified_cloud(spacing=0.03)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = "EPSG:3004"
    ch.transform = geometry.compose_similarity(1.0, np.eye(3), np.round(xyz.mean(0)))
    doc.save(tmp_path / "scavo.ppg")
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb, classification=codes)
    return ch, xyz, codes


ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # of make_classified_cloud
BUSH, WALL = (0.8, 0.8), (3.0, 2.0)  # a bush (radius 0.3) and the middle of the wall (top at 0.75)


def _ground(x, y):
    return 0.05 * np.sin(1.1 * x) + 0.04 * np.cos(0.9 * y)


def _dem_at(path, x, y):
    import rasterio

    with rasterio.open(path) as src:
        return float(next(src.sample([(ORIGIN[0] + x, ORIGIN[1] + y)]))[0])


def test_point_classes_and_export_by_class(excavation, tmp_path):
    ch, xyz, codes = excavation
    found, counts = np.unique(codes, return_counts=True)
    assert ch.point_classes() == {int(c): int(n) for c, n in zip(found, counts)}
    out = ch.export_point_cloud(tmp_path / "muro.las", classes=[64])
    wall = PointCloud.load(out)
    assert len(wall) == (codes == 64).sum() and (wall.classification == 64).all()
    assert np.allclose(np.sort(wall.xyz[:, 0]), np.sort(xyz[codes == 64, 0]), atol=1e-3)  # world coordinates
    with pytest.raises(ValueError):
        ch.export_point_cloud(tmp_path / "none.las", classes=[99])


def test_dem_of_the_ground_only(excavation):
    """A DEM from class 2 alone is a terrain model: no bushes, no wall, gaps interpolated."""
    ch, _, _ = excavation
    dsm = ch.build_dem(resolution=0.05)
    bush_top, wall_top = _dem_at(dsm, *BUSH), _dem_at(dsm, *WALL)
    dtm = ch.build_dem(resolution=0.05, classes=[2])
    assert ch.meta["dem"]["classes"] == [2]
    for (x, y), top in ((BUSH, bush_top), (WALL, wall_top)):
        ground = ORIGIN[2] + _ground(x, y)
        assert abs(_dem_at(dtm, x, y) - ground) < 0.02
        assert top - ground > 0.3
    with pytest.raises(ValueError):
        ch.build_dem(resolution=0.05, source="model", classes=[2])


def test_hidden_cells_of_the_orthomosaic(excavation):
    from pyarchinit_photogrammetry.engine.ortho import hidden_cells

    ch, _, _ = excavation
    res = 0.02
    origin = (ORIGIN[0], ORIGIN[1] + 4.0)
    shape = (int(4.0 / res), int(6.0 / res))
    hidden = hidden_cells(ch, origin, shape, res, [3, 71])

    def cell(x, y):
        return hidden[int((4.0 - y) / res), int(x / res)]

    assert cell(*BUSH) and cell(5.2, 3.2)  # bushes
    assert not cell(*WALL) and not cell(2.5, 1.0) and not cell(5.9, 2.0)  # wall, bare ground
    assert 0.02 < hidden.mean() < 0.2


def test_mesh_and_sections_without_classes(excavation):
    from pyarchinit_photogrammetry.archaeo import sections

    ch, _, _ = excavation
    x0, y0 = BUSH
    ground = ORIGIN[2] + _ground(x0, y0)
    trace = ((ORIGIN[0] + x0 - 0.6, ORIGIN[1] + y0), (ORIGIN[0] + x0 + 0.6, ORIGIN[1] + y0))
    # the dense points along the trace
    with_bush = sections.section(ch, *trace, source="point_cloud", thickness=0.04)
    ground_only = sections.section(ch, *trace, source="point_cloud", thickness=0.04, classes=[2])
    assert with_bush.points[:, 1].max() - ground > 0.3 and ground_only.points[:, 1].max() - ground < 0.03

    def top_near_bush(mesh):
        world = ch.to_world(mesh.vertices)
        near = np.hypot(world[:, 0] - ORIGIN[0] - x0, world[:, 1] - ORIGIN[1] - y0) < 0.1
        return world[near, 2].max() if near.any() else ground

    with pytest.raises(ValueError):
        ch.build_model(source="tie_points", classes=[2])
    mesh = ch.build_model(surface="height_field", classes=[2, 64])
    assert ch.meta["model"]["classes"] == [2, 64] and top_near_bush(mesh) - ground < 0.05
    mesh = ch.build_model(surface="height_field")
    assert top_near_bush(mesh) - ground > 0.3
    # the cut of the model keeps only the stretches next to points of the chosen classes
    full = sections.section(ch, *trace)
    bare = sections.section(ch, *trace, classes=[2])
    top = lambda s: max(p[:, 1].max() for p in s.polylines)  # noqa: E731
    assert top(full) - ground > 0.3 and top(bare) - ground < 0.1  # the foot of the bush may stay
    plan = sections.plan_slice(ch, ORIGIN[2] + 0.3, classes=[64])  # through the wall and the bushes
    xs = np.vstack(plan.polylines)[:, 0] - ORIGIN[0]
    assert len(xs) and xs.min() > 1.4 and xs.max() < 4.6  # the wall only


def test_classes_from_stratigraphic_units(excavation, tmp_path):
    from shapely import intersects_xy
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    ch, xyz, codes = excavation
    us1 = UnitPolygon(box(ORIGIN[0], ORIGIN[1], ORIGIN[0] + 1.4, ORIGIN[1] + 4.0), us="1", area="1", sito="Saggio")
    us2 = UnitPolygon(box(ORIGIN[0] + 4.6, ORIGIN[1], ORIGIN[0] + 6.0, ORIGIN[1] + 4.0), us="2", area="1",
                      sito="Saggio")
    rows = classify.classify_by_units(ch, [us1, us2])
    assert [r["name"] for r in rows] == ["US 1 (area 1)", "US 2 (area 1)"] and [r["code"] for r in rows] == [100, 101]
    in1 = intersects_xy(us1.geometry, xyz[:, 0], xyz[:, 1])
    pc = ch.point_cloud
    assert rows[0]["points"] == in1.sum() and (pc.classification[in1] == 100).all()
    assert rows[0]["area_m2"] == pytest.approx(1.4 * 4.0) and rows[0]["sito"] == "Saggio"
    assert rows[0]["z_min"] == pytest.approx(xyz[in1, 2].min()) and rows[0]["z_max"] == pytest.approx(xyz[in1, 2].max())
    assert ch.meta["classes"]["101"] == "US 2 (area 1)"
    # drawn again smaller: same code, the old extent goes back to unclassified, hand labels stay
    hand = np.nonzero(in1)[0][:5]
    classify.set_class(ch, hand, 70)
    small = UnitPolygon(box(ORIGIN[0], ORIGIN[1], ORIGIN[0] + 0.7, ORIGIN[1] + 4.0), us="1", area="1", sito="Saggio")
    rows = classify.classify_by_units(ch, [small])
    pc = ch.point_cloud
    now = intersects_xy(small.geometry, xyz[:, 0], xyz[:, 1])
    assert rows[0]["code"] == 100 and (pc.classification[hand] == 70).all()
    assert (pc.classification[now & ~pc.manual] == 100).all() and (pc.classification[in1 & ~now & ~pc.manual] == 1).all()
    out = classify.units_table_csv(rows, tmp_path / "us.csv")
    lines = out.read_text().splitlines()
    assert lines[0].startswith("sito;area_s;us;name;code;points") and lines[1].startswith("Saggio;1;1;US 1 (area 1);100;")
    assert classify.assign_class_by_polygons(ch, [us1, us2]) == {"1": 100, "2": 101}


def test_orthomosaic_without_a_class(processed, tmp_path):
    """Where the visible surface belongs to a hidden class the orthomosaic is transparent."""
    import shutil
    from pathlib import Path

    import rasterio

    import pyarchinit_photogrammetry as ppg

    doc0, _, _ = processed
    src = Path(doc0.path)
    shutil.copytree(src.parent, tmp_path / "copia", ignore=shutil.ignore_patterns("dense"))
    doc = ppg.Document.open(tmp_path / "copia" / src.name)
    ch = doc.chunks[0]
    pc = ch.point_cloud
    world = ch.to_world(pc.xyz)
    cx, cy = np.median(world[:, 0]), np.median(world[:, 1])
    tool = (np.abs(world[:, 0] - cx) < 0.2) & (np.abs(world[:, 1] - cy) < 0.2)  # "excavation equipment"
    pc.classification[:] = 2
    pc.classification[tool] = 71
    ch.point_cloud = pc
    path = ch.build_orthomosaic(resolution=0.01, hide_classes=[71])
    with rasterio.open(path) as r:
        alpha, inv = r.read(4), ~r.transform

    def a(x, y):
        col, row = inv @ (x, y)
        return alpha[int(row), int(col)]

    assert a(cx, cy) == 0 and a(cx + 0.6, cy) == 255 and a(cx - 0.6, cy - 0.3) == 255
    assert ch.meta["orthomosaic"]["hidden_classes"] == [71]


def test_command_line_with_classes(excavation, tmp_path):
    from shapely.geometry import box
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.cli import app

    ch, _, codes = excavation
    unit = UnitPolygon(box(ORIGIN[0], ORIGIN[1], ORIGIN[0] + 1.4, ORIGIN[1] + 4.0), us="5", area="2", sito="Saggio")
    ch.meta["drawings"] = {"units": [unit.to_dict()], "quote": []}
    ch.document.save()
    project = str(ch.document.path)
    run = CliRunner().invoke
    res = run(app, ["dem", project, "--resolution", "0.05", "--classes", "2"])
    assert res.exit_code == 0, res.output
    res = run(app, ["export", project, "--cloud", str(tmp_path / "muro.las"), "--classes", "64"])
    assert res.exit_code == 0, res.output
    assert len(PointCloud.load(tmp_path / "muro.las")) == (codes == 64).sum()
    res = run(app, ["classify-units", project, "--csv", str(tmp_path / "us.csv")])
    assert res.exit_code == 0, res.output
    assert "100 US 5 (area 2)" in res.output and (tmp_path / "us.csv").exists()
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(project).chunks[0]
    assert again.meta["dem"]["classes"] == [2] and again.meta["classes"]["100"] == "US 5 (area 2)"
    assert 100 in again.point_classes()
    res = run(app, ["dem", project, "--classes", "2,x"])
    assert res.exit_code != 0
