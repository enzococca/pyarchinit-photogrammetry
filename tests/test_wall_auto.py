"""Automatic tracing of a wall on the plan (``engine.wall_auto``): the line followed between two clicks along the
crest of the wall on a cost map of the DEM's local relief and the orthophoto's edges, then snapped onto the face;
the walls proposed on the whole plan; SAM 2.1 (a fake segmenter here) where the DEM is weak."""
from __future__ import annotations

import sys

import numpy as np
import pytest

from wall_helpers import SHIFT, model_doc, share_within, write_plan

CELL = 0.02  # m: the plan of the synthetic wall
HALF = 15.0  # m: half the ground mat, the corner of the plan at (-HALF, HALF) in the scene's metres


def _scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_wall_scene

    return make_wall_scene()


def crest_of(scene) -> np.ndarray:
    """The crest of the wall in the coordinates of the chunk: the face moved half the thickness into the wall."""
    return (np.asarray(scene.face, float) + np.asarray(scene.back, float)) / 2.0 + np.asarray(SHIFT[:2])


def _cells(x, y):
    """The (row, col) of the plan cell of the scene's point (x, y)."""
    return int((HALF - y) / CELL), int((x + HALF) / CELL)


def plan_doc(folder, edit=None, rgb=None):
    """The model of the L wall and its plan as the products of its chunk; ``edit(dem)`` changes the DEM (local
    metres, row 0 at the top) before it is written, ``rgb`` replaces the orthophoto. Returns ``(chunk, scene)``."""
    from pyarchinit_photogrammetry.testing.synthetic import wall_plan

    scene = _scene()
    _doc, ch, _info = model_doc(folder, scene)
    dem, colours, top_left = wall_plan(scene, CELL)
    dem = dem.astype(np.float64)
    if edit is not None:
        edit(dem)
    write_plan(ch, dem, colours if rgb is None else rgb, top_left, CELL)
    return ch, scene


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    return plan_doc(tmp_path_factory.mktemp("wall_auto"))


def test_the_line_between_two_clicks_follows_the_crest_and_snaps_to_the_face(plan):
    from pyarchinit_photogrammetry.engine import wall_auto as A
    from pyarchinit_photogrammetry.engine import wall_trace as T

    ch, scene = plan
    crest, face = crest_of(scene), np.asarray(scene.face, float) + np.asarray(SHIFT[:2])
    r = A.follow(ch, [crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])  # two clicks near the wall's ends
    assert share_within(r["points"], crest, 0.2) >= 0.9  # spec 9: 0.2 m along 90 %
    assert r["on_ridge"] > 0.9
    assert r["cell"] == pytest.approx(A.MIN_CELL)
    assert len(r["points"]) < 20  # simplified: a trace of a few vertices, not one per cell
    snapped = T.snap_trace(ch, r["points"], side="right")["points"]
    assert share_within(snapped, face, 0.02) >= 0.9


def test_via_points_are_passed_through(plan):
    from pyarchinit_photogrammetry.engine import wall_auto as A
    from wall_helpers import distance_to

    ch, scene = plan
    crest = crest_of(scene)
    via = crest[1] + [-1.0, 0.0]  # on the crest, a metre before the corner
    r = A.follow(ch, [crest[0] + [0.3, 0.1], via, crest[-1] + [-0.1, -0.3]])
    assert distance_to(r["points"], via[None])[0] < 0.1
    assert share_within(r["points"], crest, 0.2) >= 0.9


def test_clicks_on_open_ground_give_a_weak_line(plan):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    ch, _scene = plan
    a, b = np.array([0.0, -5.0]) + SHIFT[:2], np.array([4.0, -5.0]) + SHIFT[:2]  # 5 m from the wall
    r = A.follow(ch, [a, b])
    assert r["on_ridge"] < 0.5
    assert len(r["points"]) >= 2
    assert np.allclose(r["points"][0], a) and np.allclose(r["points"][-1], b)


def test_what_cannot_be_followed_is_said(plan, tmp_path):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    ch, scene = plan
    crest = crest_of(scene)
    _doc, bare, _info = model_doc(tmp_path / "bare", scene)
    with pytest.raises(RuntimeError, match="DEM"):
        A.follow(bare, [crest[0], crest[-1]])
    with pytest.raises(ValueError):
        A.follow(ch, [crest[0], crest[0] + [100.0, 0.0]])  # outside the DEM
    with pytest.raises(ValueError):
        A.follow(ch, [crest[0]])  # one point

    def strip(dem):  # a strip of no data 0.6 m wide across the long arm of the wall
        from pyarchinit_photogrammetry.engine.dem import NODATA

        _r, c = _cells(3.0, 0.0)
        dem[:, c:c + 30] = NODATA - SHIFT[2]  # write_plan adds the chunk's height
    holed, _s = plan_doc(tmp_path / "strip", edit=strip)
    r = A.follow(holed, [crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]])
    assert len(r["points"]) >= 2
    assert share_within(r["points"], crest, 0.2) >= 0.9


def test_the_cost_of_a_step():
    from pyarchinit_photogrammetry.engine import wall_auto as A

    strength = np.zeros((5, 5))
    strength[2] = 1.0
    rows, cols = A.least_cost(strength, (2, 0), (2, 4), 0.1)
    assert list(rows) == [2, 2, 2, 2, 2] and list(cols) == [0, 1, 2, 3, 4]

    def cost(rows, cols):  # each move: its length times the mean of 1 + COST_GAIN (1 - strength) at its two cells
        c = 1.0 + A.COST_GAIN * (1.0 - strength)
        return sum(0.1 * np.hypot(r1 - r0, c1 - c0) * (c[r0, c0] + c[r1, c1]) / 2.0
                   for r0, c0, r1, c1 in zip(rows[:-1], cols[:-1], rows[1:], cols[1:]))

    assert cost(rows, cols) == pytest.approx(0.4)
    rows, cols = A.least_cost(strength, (1, 0), (1, 4), 0.1)  # off the ridge: down to it, along it, back up
    assert list(rows) == [1, 2, 2, 2, 2, 2, 1] and list(cols) == [0, 0, 1, 2, 3, 4, 4]
    assert cost(rows, cols) == pytest.approx(0.1 * (2 * 11.0 + 4.0))
    assert cost(rows, cols) < cost([1, 2, 2, 2, 1], [0, 1, 2, 3, 4])  # cutting the corners on the ground costs more
    assert cost(rows, cols) < cost([1] * 5, [0, 1, 2, 3, 4])


def test_the_strength_map():
    from pyarchinit_photogrammetry.engine import wall_auto as A

    dem = np.zeros((200, 200))
    dem[95:105, 20:180] = 1.0  # a wall 0.5 m wide, 1 m high, at 0.05 m
    dem[0:5, 0:5] = np.nan
    s = A.strength_map(dem, 0.05)
    assert s.min() >= 0.0 and s.max() <= 1.0
    assert s[100, 100] > 0.9 and s[50, 100] == 0.0 and s[2, 2] == 0.0
    mask = np.zeros_like(dem, bool)
    mask[50, 100] = True
    assert A.strength_map(dem, 0.05, mask=mask)[50, 100] == pytest.approx(A.MASK_STRENGTH)
    grey = np.zeros_like(dem)
    grey[:, 100:] = 255.0  # an edge in the orthophoto, on the ground
    e = A.strength_map(dem, 0.05, ortho=grey)
    assert e[30, 100] == pytest.approx(A.EDGE_WEIGHT, abs=0.05) and e[30, 50] == 0.0


def test_proposals_are_the_walls_and_only_them(tmp_path):
    from pyarchinit_photogrammetry.engine import wall_auto as A

    second = np.array([[-8.0, 8.0], [-3.0, 8.0]])  # a straight wall of 5 m, 1 m high, 0.4 m thick

    def edit(dem):
        r0, c0 = _cells(-8.0, 8.2)
        r1, c1 = _cells(-3.0, 7.8)
        dem[r0:r1, c0:c1] = 1.0
        r0, c0 = _cells(-8.0, -6.0)
        r1, c1 = _cells(-6.0, -8.0)
        dem[r0:r1, c0:c1] += 0.1  # a bump of 0.1 m: no wall
    ch, scene = plan_doc(tmp_path, edit=edit)
    found = A.proposals(ch)
    assert len(found) == 2
    crests = [crest_of(scene), second + SHIFT[:2]]
    for line in found:
        assert len(line) < 20  # simplified
        length = float(np.hypot(*np.diff(line, axis=0).T).sum())
        assert length >= 3.0
        assert max(share_within(line, c, 0.2) for c in crests) >= 0.9
    for c in crests:  # each wall proposed
        assert max(share_within(line, c, 0.2) for line in found) >= 0.9
    bump = np.array([-7.0, -7.0]) + SHIFT[:2]
    from wall_helpers import distance_to

    assert all(distance_to(line, bump[None])[0] > 1.0 for line in found)
    assert A.proposals(ch, min_length=20.0) == []


class FakeSegmenter:
    """A segmenter that gives back the footprint of the wall on the image it is handed, whatever the points."""

    def __init__(self, footprint):
        self.footprint, self.calls = footprint, []

    def segment_points(self, image, points, labels):
        self.calls.append((np.asarray(image).shape, np.asarray(points, float), np.asarray(labels)))
        return self.footprint(np.asarray(image).shape)

    def segment_box(self, image, box):  # pragma: no cover - not used
        raise AssertionError

    def segment_auto(self, image):  # pragma: no cover - not used
        raise AssertionError


def test_sam_helps_where_the_dem_is_weak(tmp_path):
    from scipy.ndimage import gaussian_filter
    from shapely import contains_xy
    from shapely.geometry import Polygon

    from pyarchinit_photogrammetry.engine import wall_auto as A

    rng = np.random.default_rng(5)
    holder = {}

    def edit(dem):  # the wall 0.05 m high on a ground with bumps of 0.05 m
        wall = dem > 0
        holder["wall"] = wall.copy()
        noise = gaussian_filter(rng.normal(0.0, 1.0, dem.shape), 10.0)
        dem[:] = np.where(wall, 0.05, 0.0) + 0.05 * noise / noise.std()
    plain = rng.integers(140, 160, (3, 1500, 1500)).astype(np.uint8)  # the orthophoto does not show it either
    ch, scene = plan_doc(tmp_path, edit=edit, rgb=plain)
    crest = crest_of(scene)
    clicks = [crest[0] + [0.3, 0.1], crest[-1] + [-0.1, -0.3]]
    alone = A.follow(ch, clicks)
    assert share_within(alone["points"], crest, 0.2) < 0.5

    foot = Polygon(np.vstack([scene.face, scene.back[::-1]]) + np.asarray(SHIFT[:2]))
    holder_tr = {}

    def footprint(shape):
        tr = holder_tr["tr"]
        rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
        x, y = tr @ (cols + 0.5, rows + 0.5)
        return contains_xy(foot, x, y)

    import pyarchinit_photogrammetry.ai.sam as S

    real = S.read_orthomosaic

    def read(*a, **k):
        out = real(*a, **k)
        holder_tr["tr"] = out[2]
        return out
    seg = FakeSegmenter(footprint)
    import unittest.mock as um

    with um.patch.object(S, "read_orthomosaic", read):
        mask, tr = A.sam_mask(ch, seg, clicks)
    assert len(seg.calls) == 1
    shape, points, labels = seg.calls[0]
    assert max(shape[:2]) <= 1536
    assert np.all(points >= 0) and np.all(points[:, 0] < shape[1]) and np.all(points[:, 1] < shape[0])
    assert np.all(labels == 1)
    helped = A.follow(ch, clicks, mask=(mask, tr))
    assert share_within(helped["points"], crest, 0.2) >= 0.9
    assert "torch" not in sys.modules
