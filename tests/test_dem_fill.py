"""The ground of a survey with dense and sparse parts - the centre of a drone survey seen by dozens
of photos, its margins by two or three - becomes one DEM: the sparse ground is interpolated at the
scale of its points (not left as scattered dots), stray points there do not become cones, the stray
points around the survey stay out and the measured cells are not touched."""
from __future__ import annotations

import numpy as np

from pyarchinit_photogrammetry.engine.dem import covered_ground, dem_from_points, remove_spikes, remove_stray_cells


def _plane(x, y):
    return 100.0 + 0.1 * x + 0.05 * y


def test_stray_cells_off_the_slope_go_the_others_stay():
    rng = np.random.default_rng(3)
    res = 0.1
    dem = np.full((300, 300), np.nan)
    cells = np.argwhere(rng.random(dem.shape) < 0.01)  # a cell in a hundred: no neighbour around most
    x, y = cells[:, 1] * res, -cells[:, 0] * res
    dem[cells[:, 0], cells[:, 1]] = _plane(x, y) * 3 - 200 + rng.normal(0, 0.01, len(cells))  # a 30 % slope
    stray = rng.choice(len(cells), 20, replace=False)
    dem[cells[stray, 0], cells[stray, 1]] += rng.choice([-1.0, 1.0], 20)
    out, spikes = remove_spikes(dem, res)  # strays with a neighbour
    out, removed = remove_stray_cells(out, res)  # strays alone
    after = out[cells[:, 0], cells[:, 1]]
    truth = _plane(x, y) * 3 - 200
    assert (np.isnan(after[stray]) | (np.abs(after[stray] - truth[stray]) < 0.2)).all()
    kept = np.setdiff1d(np.arange(len(cells)), stray)
    assert np.isnan(after[kept]).sum() <= 3  # the slope itself is no reason


def test_dense_and_sparse_ground_make_one_dem():
    rng = np.random.default_rng(0)
    res = 0.1
    dense = rng.uniform([0, 0], [50, 100], (200_000, 2))  # 40 points per m2
    sparse = rng.uniform([50, 0], [100, 100], (4_000, 2))  # 0.8 per m2: a point every 1.1 m (11 cells)
    xy = np.vstack([dense, sparse])
    pts = np.c_[xy, _plane(xy[:, 0], xy[:, 1]) + rng.normal(0, 0.01, len(xy))]
    stray = rng.choice(len(pts), 30, replace=False)
    pts[stray, 2] += 3.0
    outside = np.c_[rng.uniform(120, 200, (20, 2)), np.full(20, 100.0)]  # stray points far from the survey
    dem, origin, spikes = dem_from_points(np.vstack([pts, outside]), res)
    rows, cols = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    x, y = origin[0] + (cols + 0.5) * res, origin[1] - (rows + 0.5) * res
    survey = (x < 100) & (y < 100)
    err = np.abs(dem - _plane(x, y))
    assert np.isfinite(dem[survey]).mean() > 0.98  # the sparse half too, not scattered dots
    inside = (x > 4) & (x < 96) & (y > 4) & (y < 96)
    assert np.nanpercentile(err[inside], 99) < 0.1
    assert np.nanmax(err[inside]) < 0.3  # stray points of 3 m are not cones
    # along the edges of the sparse half, a point every metre, the slope is followed less closely
    edge = survey & (x > 1) & (x < 99) & (y > 1) & (y < 99) & ~inside
    assert np.nanpercentile(err[edge], 99) < 0.3
    assert spikes >= 25
    assert np.isnan(dem[(x > 110) & (y > 110)]).all()  # the points far around stay out


def test_covered_ground_ends_near_the_points():
    mask = np.zeros((400, 400), bool)
    mask[100:300:8, 100:300:8] = True  # sparse ground: a point every 8 cells
    mask[350, 30] = True  # a stray point
    ground = covered_ground(mask, coarse=16)
    assert ground[110:290, 110:290].all()  # the gaps between the points are ground
    assert not ground[350, 30] and not ground[:60].any()  # the stray point and the empty side are not
    assert not ground[100:300, 330:].any()  # at most a coarse cell beyond the last point


def test_pits_of_the_thin_margin_go_the_trench_of_the_dense_part_stays():
    """A survey DEM at 0.2 m: a dense part with a 2.5 m deep, 4 m wide trench, and a thin margin
    (one cell in twenty) with a cluster of cells 8 m below the ground: the cluster is emptied, the
    trench and the rest keep their heights."""
    from pyarchinit_photogrammetry.engine.dem import remove_pits

    rng = np.random.default_rng(0)
    res = 0.2
    dem = 100.0 + 0.001 * np.arange(300)[:, None] + rng.normal(0, 0.02, (300, 300))
    dem[140:160, :150] -= 2.5  # the trench, in the dense part
    thin = rng.random((300, 300)) > 0.05
    dem[:, 150:] = np.where(thin[:, 150:], np.nan, dem[:, 150:])  # the margin: one cell in twenty
    dem[200:203, 220:223] = 92.0  # wrong points at the margin
    before = dem.copy()
    out, n = remove_pits(dem, res)
    assert 5 <= n <= 9 and np.isnan(out[200:203, 220:223]).all()
    rest = ~np.isnan(before)
    rest[200:203, 220:223] = False
    assert np.array_equal(out[rest], before[rest])  # the trench, the dense part and the good margin cells
