import numpy as np
import pytest
import trimesh

from pyarchinit_photogrammetry.archaeo import pottery, sections, terrain
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.testing.synthetic import make_vessel


def _vessel(**kw):
    return make_vessel(**kw)


def test_pottery_axis_profile_and_drawing(tmp_path):
    from scipy.spatial.transform import Rotation

    mesh = _vessel()
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=4).as_matrix()
    t[:3, 3] = [1, 2, 3]
    mesh.apply_transform(t)
    axis = pottery.estimate_axis(mesh)
    assert np.degrees(np.arccos(abs(axis.direction @ t[:3, 2]))) < 0.5
    prof = pottery.best_profile(mesh, axis)
    meas = prof.measurements()
    assert abs(meas["rim_diameter_m"] - 0.16) < 0.003
    assert abs(meas["height_m"] - 0.12) < 0.002
    assert abs(meas["wall_thickness_m"] - 0.006) < 0.0015
    assert pottery.draw(prof, tmp_path / "p.svg").read_text().startswith("<svg")
    assert pottery.draw(prof, tmp_path / "p.dxf").exists()


def test_volume_against_analytic(tmp_path):
    z = np.full((100, 200), 10.0, np.float32)
    z[20:60, 50:150] = 9.5  # a pit 1.0 x 2.0 m, 0.5 m deep at 1 cm resolution
    dem = write_geotiff(tmp_path / "dem.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)
    res = terrain.volume(dem, reference=10.0)
    assert abs(res["fill_m3"] - 0.4 * 1.0 * 0.5) < 1e-6
    lines = terrain.contours(dem, interval=0.25)
    assert lines and all(np.ptp(l["coords"][:, 0]) <= 2.0 for l in lines)
    assert terrain.export_contours(lines, tmp_path / "c.dxf").exists()
    assert terrain.hillshade(dem, tmp_path / "hs.tif").exists()


@pytest.mark.slow
def test_section_through_wall(processed, tmp_path):
    _, ch, _ = processed
    x0, y0 = 2480000.0, 4500000.0
    s = sections.section(ch, (x0 + 0.5, y0 + 1.4), (x0 + 3.5, y0 + 1.4))
    pts = np.vstack(s.polylines)
    assert pts[:, 1].max() > 99.98 and pts[:, 1].min() < 99.70  # surface ~100 m, trench ~99.65 m
    at_wall = pts[(pts[:, 0] > 1.35) & (pts[:, 0] < 1.65), 1]
    assert np.median(at_wall) == pytest.approx(99.95, abs=0.02)  # wall top
    assert s.to_dxf(tmp_path / "s.dxf").exists() and s.to_svg(tmp_path / "s.svg").exists()
    cloud = sections.section(ch, (x0 + 0.5, y0 + 1.4), (x0 + 3.5, y0 + 1.4), source="point_cloud", thickness=0.02)
    assert len(cloud.points) > 100
    plan = sections.plan_slice(ch, 99.85)
    assert plan.polylines


def test_pottery_axis_from_noisy_sherd():
    from scipy.spatial.transform import Rotation

    mesh = make_vessel(sherd=(10, 70), noise=0.0002)
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=11).as_matrix()
    mesh.apply_transform(t)
    axis = pottery.estimate_axis(mesh)
    assert np.degrees(np.arccos(abs(axis.direction @ t[:3, 2]))) < 3.0
    meas = pottery.best_profile(mesh, axis).measurements()
    assert abs(meas["rim_diameter_m"] - 0.16) < 0.004
