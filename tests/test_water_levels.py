"""Several water levels (engine/water): basins with their own level and factor, a water plane fitted to the
shoreline, the effective factor from control points of known true height, levels per chunk or per date. The synthetic
pond of test_water (no real place)."""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import water

from test_water import LEVEL, X0, Y0, basin, project


def test_plane_from_the_shoreline():
    rng = np.random.default_rng(0)
    xy = rng.uniform(-3, 3, (12, 2)) + [X0, Y0]
    z = 0.8 + 0.002 * (xy[:, 0] - X0) - 0.001 * (xy[:, 1] - Y0)
    fit = water.plane_from_shore(np.column_stack([xy, z]))
    assert fit["a"] == pytest.approx(0.002, abs=1e-9) and fit["b"] == pytest.approx(-0.001, abs=1e-9)
    assert fit["rmse"] < 1e-9 and fit["points"] == 12
    assert fit["level"] == pytest.approx(0.8 + 0.002 * (fit["x0"] - X0) - 0.001 * (fit["y0"] - Y0))
    with pytest.raises(water.WaterError, match="on a line"):
        water.plane_from_shore([[0, 0, 1], [1, 1, 1], [2, 2, 1]])
    with pytest.raises(water.WaterError, match="three points"):
        water.plane_from_shore([[0, 0, 1], [1, 0, 1]])


def test_two_basins_with_their_own_level_and_factor(tmp_path):
    doc, a = project(tmp_path)
    before = a.to_world(a.point_cloud.xyz)
    west = water.basin("Ovest", level=0.80, n=1.34, polygon=box(X0 - 10, Y0 - 10, X0, Y0 + 10))
    east = water.basin("Est", level=0.75, n=1.30, polygon=box(X0, Y0 - 10, X0 + 10, Y0 + 10))
    water.set_basins(a, [west, east])
    assert water.level_of(a)["level"] == pytest.approx(0.80)  # the first basin became the level of the chunk
    r = water.correct_cloud(a)
    assert r["name"] == "Nuvola corretta per la rifrazione (2 bacini)"
    after = a.to_world(a.point_cloud.xyz)
    w = before[:, 0] < X0
    exp = np.where(w, water.corrected(before[:, 2], 0.80, 1.34), water.corrected(before[:, 2], 0.75, 1.30))
    assert np.allclose(after[:, 2], exp, atol=1e-6)
    by = {b["name"]: b for b in r["basins"]}
    assert by["Ovest"]["below"] == int((w & (before[:, 2] < 0.80)).sum())
    assert by["Est"]["below"] == int((~w & (before[:, 2] < 0.75)).sum())
    doc.save()
    again = ppg.Document.open(doc.path).chunks[0]
    assert [b["name"] for b in water.basins_of(again)] == ["Ovest", "Est"]


def test_a_tilted_water_plane_and_points_outside_every_basin(tmp_path):
    doc, a = project(tmp_path)
    before = a.to_world(a.point_cloud.xyz)
    shore = [[X0 - 2, Y0, 0.79], [X0 + 2, Y0, 0.81], [X0, Y0 + 2, 0.80], [X0, Y0 - 2, 0.80]]
    only = water.basin("Pozza", n=1.33, shore=shore, polygon=box(X0 - 1.5, Y0 - 1.5, X0 + 1.5, Y0 + 1.5))
    water.set_basins(a, [only])
    water.correct_cloud(a)
    after = a.to_world(a.point_cloud.xyz)
    inside = (np.abs(before[:, 0] - X0) < 1.5) & (np.abs(before[:, 1] - Y0) < 1.5)
    L = 0.80 + 0.005 * (before[:, 0] - X0)
    assert np.allclose(after[inside, 2], water.corrected(before[inside, 2], L[inside], 1.33), atol=1e-6)
    assert np.array_equal(after[~inside], before[~inside])  # no basin there: untouched


def test_basins_move_with_the_chunk(tmp_path):
    doc, a = project(tmp_path)
    water.set_basins(a, [water.basin("B", level=0.8, polygon=box(X0, Y0, X0 + 1, Y0 + 1)),
                         water.basin("Resto", level=0.7)])
    t = a.transform.copy()
    t[:3, 3] += [2.0, 0.0, 1.0]
    a.transform = t
    b = water.basins_of(a)
    assert b[0]["level"] == pytest.approx(1.8) and b[1]["level"] == pytest.approx(1.7)
    L, n = water.surface_at(a, [[X0 + 2.5, Y0 + 0.5], [X0 + 0.5, Y0 + 0.5]])
    assert L.tolist() == pytest.approx([1.8, 1.7])
    with pytest.raises(water.WaterError, match="only one basin"):
        water.set_basins(a, [water.basin("x", level=1.0), water.basin("y", level=1.0)])


def test_factor_from_control_points(tmp_path):
    doc, a = project(tmp_path)
    water.set_level(a, LEVEL, source="hand", n=1.34)
    rng = np.random.default_rng(1)
    xy = rng.uniform(-1.5, 1.5, (10, 2)) + [X0, Y0]
    apparent = basin(xy[:, 0], xy[:, 1])  # the DEM is the bottom seen through the water
    true = LEVEL - 1.31 * (LEVEL - apparent) + rng.normal(0, 0.001, len(xy))
    controls = water.control_points(a, points=np.column_stack([xy, true]), source="dem")
    fit = water.estimate_factor(a, controls)
    assert fit["n"] == pytest.approx(1.31, abs=0.01) and fit["points"] == 10 and fit["rmse"] < 0.003
    assert all(abs(r["residual"]) < 0.005 for r in fit["residuals"])
    both = water.estimate_factor(a, controls, fit_level=True)
    assert both["level"] == pytest.approx(LEVEL, abs=0.03) and both["n"] == pytest.approx(1.31, abs=0.03)
    # a point above the water is left out, and said
    dry = water.control_points(a, points=[[X0 + 2.9, Y0 + 2.9, 1.5, 1.5]])
    fit2 = water.estimate_factor(a, controls + dry)
    assert fit2["left_out"] == ["P1"] and fit2["points"] == 10
    rec = water.apply_factor(a, fit)
    assert water.level_of(a)["n"] == pytest.approx(fit["n"]) and rec["factor_fit"]["points"] == 10


def test_factor_from_markers_triangulated_through_the_water(tmp_path):
    doc, a = project(tmp_path)
    water.set_level(a, LEVEL, source="hand", n=1.34)
    for k, (dx, dy) in enumerate([(0.2, 0.1), (-0.5, 0.3), (0.4, -0.6), (-0.2, -0.2)]):
        x, y = X0 + dx, Y0 + dy
        app = basin(x, y)
        m = a.add_marker(f"T{k}", position=a.to_internal(np.array([[x, y, app]]))[0])
        m.reference.location = [x, y, LEVEL - 1.32 * (LEVEL - app)]
    fit = water.estimate_factor(a, water.control_points(a, markers=["T0", "T1", "T2", "T3"]))
    assert fit["n"] == pytest.approx(1.32, abs=1e-6) and fit["rmse"] < 1e-6


def test_levels_per_chunk_and_per_date(tmp_path):
    from pyarchinit_photogrammetry.archaeo.timeline import set_phase_date

    doc, a = project(tmp_path)
    b = doc.add_chunk("2025")
    set_phase_date(a, "2024-09-02")
    set_phase_date(b, "2025-09-26")
    doc.save()
    table = tmp_path / "livelli.csv"
    table.write_text("date;level;n\n2024-09-02;0,82;1,335\n2025-09-26;0,78;1,30\n", encoding="utf-8")
    done = water.apply_table(doc, water.read_table(table))
    assert [(d["chunk"], d["level"], d["n"]) for d in done] == [("A", 0.82, 1.335), ("2025", 0.78, 1.30)]
    assert a.meta["water"]["date"] == "2024-09-02"
    with pytest.raises(water.WaterError, match="no chunk"):
        water.apply_table(doc, [{"chunk": "nessuno", "level": 1.0, "n": 1.33}])
    res = CliRunner().invoke(app, ["water", "table", str(doc.path), str(table)])
    assert res.exit_code == 0, res.output


def test_cli_basin_factor_and_show(tmp_path):
    doc, a = project(tmp_path)
    doc.save()
    runner = CliRunner()
    res = runner.invoke(app, ["water", "basin", str(doc.path), "A", "--name", "Pozza",
                              "--shore", f"{X0 - 2},{Y0},0.80;{X0 + 2},{Y0},0.80;{X0},{Y0 + 2},0.80",
                              "--polygon", box(X0 - 2, Y0 - 2, X0 + 2, Y0 + 2).wkt, "--n", "1.33"])
    assert res.exit_code == 0, res.output
    pts = tmp_path / "controlli.csv"
    xy = np.array([[X0 + 0.3, Y0], [X0 - 0.4, Y0 + 0.4], [X0, Y0 - 0.7]])
    true = LEVEL - 1.33 * (LEVEL - basin(xy[:, 0], xy[:, 1]))
    pts.write_text("label,x,y,z\n" + "".join(f"C{k},{x},{y},{z}\n" for k, ((x, y), z) in enumerate(zip(xy, true))))
    res = runner.invoke(app, ["water", "factor", str(doc.path), "A", "--points", str(pts), "--basin", "Pozza",
                              "--apply"])
    assert res.exit_code == 0, res.output
    assert "n 1.33" in res.output and "C0" in res.output
    res = runner.invoke(app, ["water", "show", str(doc.path), "A"])
    assert "Pozza" in res.output
