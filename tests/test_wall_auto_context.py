"""Automatic tracing of walls among other structures (``engine.wall_auto``): the line followed between two clicks
keeps to the crest of the wall clicked - not the crest of a parallel wall, the middle of the wall and the rubble
against it, the edge of a trench beside it or the tower built against it - and the walls proposed on the plan are
the walls, not the trench edges. Measured on the synthetic DEMs of ``wall_context_scenes``: the share of the true
crest within 10 cm of the line traced and the mean distance of the crest from it.

Before the crests were looked for on both sides (2.0.0, the local relief over 2 m): a parallel wall 0.5 m off
0 % (on the neighbour, 1.15 m away), rubble 0 % (32 cm off), a trench edge 50 %, the tower 67 %, all at once 4.5 %;
the free-standing walls 100 %. Now 100 % on every case (corner 100 %, 4.6 cm)."""
from __future__ import annotations

import numpy as np
import pytest

import wall_context_scenes as S

FOLLOW_SHARE = 0.95  # of the true crest within 10 cm of the line followed (measured: 1.00 on every case)
FOLLOW_MEAN = 0.06  # m: the mean distance of the crest from it (measured: 0.012-0.046)


@pytest.fixture(scope="module")
def cases():
    return {c.name: c for c in S.all_cases()}


@pytest.fixture(scope="module")
def folder(tmp_path_factory):
    return tmp_path_factory.mktemp("wall_context")


@pytest.mark.parametrize("name", ["free", "free_bent", "free_wide", "parallel_0.5", "parallel_1.0", "parallel_1.5",
                                  "rubble", "trench_edge", "pit_cross", "corner", "tower_beside", "robbed", "low",
                                  "crowded"])
def test_the_line_followed_keeps_to_the_crest_of_the_wall_clicked(cases, folder, name):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = cases[name]
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, mean = S.score(r["points"], case.crest)
    assert share >= FOLLOW_SHARE, (name, share, mean)
    assert mean <= FOLLOW_MEAN, (name, share, mean)
    assert r["on_ridge"] > 0.85


@pytest.mark.parametrize("name", ["parallel_0.5", "rubble", "trench_edge", "pit_cross", "crowded", "corner"])
def test_the_walls_proposed_are_the_walls(cases, folder, name):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = cases[name]
    found = A.proposals(S.chunk_of(case, folder))
    share, _mean = S.score_union(found, case.crest)
    assert share >= 0.9, (name, share)  # measured: 1.00, the corner 0.94 (its T and L cut the crest into branches)
    if name in ("rubble", "trench_edge", "pit_cross"):
        assert len(found) == 1  # neither the rubble nor the edge of the trench or of the pit


def _profile(width_cells=14, height=1.0):
    """A DEM 200 x 200 at 0.05 m with a wall along the rows 93..106 (``width_cells`` wide, ``height`` high)."""
    dem = np.zeros((200, 200))
    r0 = 100 - width_cells // 2
    dem[r0:r0 + width_cells, 10:190] = height
    return dem, r0


def test_the_crest_is_higher_than_the_ground_on_both_sides():
    from pyarchinit_photogrammetry.engine import wall_auto as A

    dem, r0 = _profile()
    rubble = dem.copy()
    rows = np.arange(r0 + 14, r0 + 14 + 40)  # rubble against the south face: 0.7 m falling to 0 over 2 m
    rubble[rows, 10:190] = np.maximum(rubble[rows, 10:190], (0.7 * (1 - (rows - rows[0]) / 40.0))[:, None])
    h, centred, walled = A.crest_map(rubble, 0.05)
    assert h[100, 100] > 0.8 and centred[100, 100] == pytest.approx(1.0)  # the ground within 2 m on the rubble side
    assert h[r0 + 16, 100] < 0.05  # the rubble stands against the wall: not a crest, however much it falls outwards
    assert not walled[r0 + 16, 100] or h[r0 + 16, 100] < A.RIDGE_HEIGHT * A.RIDGE

    trench = dem.copy()
    trench[r0 - 50:r0 - 10, :] -= 1.3  # a trench 0.5 m beyond the north face
    h, centred, walled = A.crest_map(trench, 0.05)
    # the strip between the wall and the trench is no crest: at most a ledge against the wall, at half strength
    assert h[r0 - 5, 100] < 0.05 or walled[r0 - 5, 100]
    assert A.strength_map(trench, 0.05)[r0 - 5, 100] <= A.STEP_WEIGHT + 1e-9
    assert h[100, 100] == pytest.approx(1.0, abs=0.05) and centred[100, 100] == pytest.approx(1.0)

    parallel = dem.copy()
    parallel[r0 - 10 - 16:r0 - 10, 10:190] = 1.5  # a taller, wider wall 0.5 m off
    s = A.strength_map(parallel, 0.05)
    assert s[100, 100] == pytest.approx(1.0, abs=0.05)  # its own middle as strong as the neighbour's
    assert s[r0 - 5, 100] == 0.0  # the gap between them is ground


def test_a_wall_against_a_tower_is_a_weaker_crest_not_none():
    from pyarchinit_photogrammetry.engine import wall_auto as A

    dem, r0 = _profile()
    dem[r0 - 60:r0, 80:120] = 3.0  # a tower 2 m higher against the north face
    h, centred, walled = A.crest_map(dem, 0.05)
    assert walled[100, 100] and h[100, 100] == pytest.approx(1.0, abs=0.05)
    s = A.strength_map(dem, 0.05)
    assert 0.3 < s[100, 100] <= A.STEP_WEIGHT + 1e-9
    assert s[100, 40] == pytest.approx(1.0, abs=0.05)  # away from the tower the wall is a crest again
    assert not walled[r0 + 16, 100]  # the ground at the foot of the south face is no top against a structure


def test_stones_on_a_top_do_not_break_the_crest():
    from pyarchinit_photogrammetry.engine import wall_auto as A

    dem, r0 = _profile(width_cells=16)
    rng = np.random.default_rng(4)
    stones = rng.uniform(-0.08, 0.08, (8, 90)).repeat(2, axis=0).repeat(2, axis=1)  # blocks of 0.1 m, +-8 cm
    dem[r0:r0 + 16, 10:190] += stones
    s = A.strength_map(dem, 0.05)
    middle = s[r0 + 7:r0 + 9, 20:180].max(axis=0)
    assert (middle > 0.8).mean() > 0.95


# ---------------------------------------------------------------- other angles, cells, no data, coarse plans
VARIANT_CASES = ("free", "parallel_0.5", "rubble", "trench_edge", "crowded")
MAKERS = {"free": S.free_wall, "parallel_0.5": lambda: S.parallel(0.5), "rubble": S.rubble,
          "trench_edge": S.trench_edge, "crowded": S.crowded, "tower_beside": S.tower_beside}
OBLIQUE_MEAN = 0.08  # m: on a wall across the grid the path is a staircase of cells, simplified within 0.1 m


@pytest.mark.parametrize("rot", [30.0, 45.0, 60.0])
@pytest.mark.parametrize("name", VARIANT_CASES)
def test_walls_across_the_grid_are_followed_as_well(folder, name, rot):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    with S.view(rot=rot):
        case = MAKERS[name]()
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, mean = S.score(r["points"], case.crest)
    assert share >= 0.9 and mean <= OBLIQUE_MEAN, (name, rot, share, mean)


@pytest.mark.parametrize("cell", [0.02, 0.10])
@pytest.mark.parametrize("name", VARIANT_CASES)
def test_dems_of_other_cells_are_followed_as_well(folder, name, cell):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    with S.view(cell=cell):
        case = MAKERS[name]()
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, mean = S.score(r["points"], case.crest)
    assert share >= FOLLOW_SHARE and mean <= FOLLOW_MEAN, (name, cell, share, mean)


@pytest.mark.parametrize("name", ["rubble", "crowded"])
def test_holes_in_the_dem_do_not_lose_the_wall(folder, name):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = S.holed(MAKERS[name]())
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, mean = S.score(r["points"], case.crest)
    assert share >= 0.9 and mean <= FOLLOW_MEAN, (name, share, mean)


@pytest.mark.parametrize("name", ["free", "crowded"])
def test_walls_are_proposed_on_the_coarse_grid_of_a_large_plan(folder, name):
    """A plan of 600 m is read at about 0.3 m (MAX_CELLS): a wall 0.7 m thick is 2-3 cells of crest there."""
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = S.coarsened(MAKERS[name](), 6)  # 0.30 m
    found = A.proposals(S.chunk_of(case, folder))
    share, _mean = S.score_union(found, case.crest, tolerance=0.3)
    assert share >= 0.85, (name, len(found), share)


def test_a_wall_against_a_taller_structure_is_not_called_weak(folder):
    """The line along a wall against a tower or a baulk is right: ``on_ridge`` must not call it weak."""
    from pyarchinit_photogrammetry.engine import wall_auto as A

    b = S._Builder(12)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    b.z = np.where(b.y > 15.35, np.maximum(b.z, b.ground + 0.9 + 0.6), b.z)  # a baulk 0.6 m over its top, north
    case = S._case("baulk", b, line)
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, _mean = S.score(r["points"], case.crest)
    assert share >= FOLLOW_SHARE
    assert r["on_ridge"] > 0.85


@pytest.mark.xfail(reason="a wall still buried on one side up to about its top is no crest (documented limit)",
                   strict=True)
def test_a_wall_bared_on_one_face_only(folder):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    b = S._Builder(13)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    b.z = np.where(b.y > 15.35, np.maximum(b.z, b.ground + 0.9 + 0.1), b.z)  # the baulk 0.1 m over its top
    case = S._case("one_face", b, line)
    share, _mean = S.score(A.follow(S.chunk_of(case, folder), case.clicks)["points"], case.crest)
    assert share >= 0.9


def test_the_crests_can_be_cancelled():
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
    from pyarchinit_photogrammetry.engine import wall_auto as A

    seen = []
    p = Progress(lambda f, m: seen.append(f))
    dem, _r0 = _profile()
    A.crest_map(dem, 0.05, progress=p)
    assert len(seen) >= 4  # a step for each direction
    p.cancel()
    with pytest.raises(Cancelled):
        A.crest_map(dem, 0.05, progress=p)


def test_corner_proposals_on_10_cm_cells(folder):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = S.coarsened(S.corner(), 2)  # the 5 cm DEM read at 10 cm, as a larger plan is
    share, _mean = S.score_union(A.proposals(S.chunk_of(case, folder)), case.crest, tolerance=0.15)
    # within 10 cm the half cell of a 10 cm grid and the 10 cm simplification decide (0.49 with the opening, 0.97
    # without): within 15 cm 1.00 either way; the test keeps the corner's three walls proposed on 10 cm cells
    assert share >= 0.9, share


def test_a_wall_tangent_to_a_curved_tower_is_followed(cases, folder):
    """Like S Nord at Khutm: the line stays on the wall where it runs along the curved wall of a round tower."""
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = S.tower_tangent()
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, mean = S.score(r["points"], case.crest)
    assert share >= FOLLOW_SHARE and mean <= FOLLOW_MEAN, (share, mean)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_zig_zags_on_a_wide_rough_top(folder, seed):
    """A top 2 m wide of boulders (+-25 cm): the followed line used to zig-zag across it (15 % longer)."""
    from pyarchinit_photogrammetry.engine import wall_auto as A

    case = S.wide_rough(seed, rough=0.25)
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    length = float(np.hypot(*np.diff(r["points"], axis=0).T).sum())
    assert length / float(np.hypot(*np.diff(case.clicks, axis=0).T).sum()) <= 1.05
    share, _mean = S.score(r["points"], case.crest, tolerance=0.25)
    assert share >= 0.8, share


def test_a_narrow_corner_beside_a_wide_wall_is_not_evened(folder):
    """An L of a wall 0.6 m thick with a wall 2.5 m thick 0.3 m beyond its corner: the line keeps to the narrow top
    (the evening of wide tops must take the width of the top the line is on, not of the neighbour)."""
    from pyarchinit_photogrammetry.engine import wall_auto as A

    b = S._Builder(16)
    crest = [(8, 15), (20, 15), (20, 24)]
    b.wall(crest, 0.6, 0.9)
    b.wall([(21.85, 9), (21.85, 14.4)], 2.5, 1.2)  # its face 0.3 m beyond the corner, south of it
    case = S._case("narrow_l_wide", b, crest)
    r = A.follow(S.chunk_of(case, folder), case.clicks)
    share, _mean = S.score(r["points"], case.crest)
    assert share >= 0.98, share  # 0.995 without the evening; 0.936-0.940 when it took the neighbour's width
    corner = np.array([20.0, 15.0]) + [S.ORIGIN[0], S.ORIGIN[1] - S.SIZE[1]]
    near = S._sample(case.crest)
    near = near[np.hypot(*(near - corner).T) < 1.5]
    assert S._distance_to(r["points"], near).max() <= 0.36  # 0.32 m at the inner corner without it, 0.43-0.45 with
