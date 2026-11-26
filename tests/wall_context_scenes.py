"""Synthetic DEMs of walls among other structures, for the automatic tracing (``engine.wall_auto``): a wall beside a
parallel wall, with rubble against one face, on the edge of a trench, across a pit, meeting other walls and a tower,
a low wall on uneven ground, and free-standing walls. Each case is a DEM (numpy, row 0 at the top) with the true
crest of the target wall (the middle line of its top) and the two clicks at its ends; the DEM is written as a
GeoTIFF and handed to the tracer through a minimal chunk (``dem_path``, ``orthomosaic_path``)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from contextlib import contextmanager

import numpy as np

CELL = 0.05  # m: the DEM of the cases (the tracer's MIN_CELL)
CENTRE = (16.0, 15.0)  # local m: the cases are turned about this point
_VIEW = {"cell": CELL, "rot": 0.0}  # how the cases are built: their cell and the angle they are turned by (deg)
ORIGIN = (500000.0, 4400030.0)  # the top-left corner of every case, in a projected system
SIZE = (32.0, 30.0)  # m: width, height of every case


@dataclass
class Case:
    name: str
    dem: np.ndarray  # (h, w) metres, row 0 at the top (north)
    crest: np.ndarray  # (N, 2) the true crest of the target wall, world x, y
    clicks: np.ndarray  # (2, 2) the clicks at its ends, world x, y
    cell: float = CELL


@contextmanager
def view(cell: float = CELL, rot: float = 0.0):
    """The cases built inside this block on cells of ``cell`` m and turned by ``rot`` degrees (anticlockwise) about
    :data:`CENTRE`: their crests and clicks turned with them."""
    old = dict(_VIEW)
    _VIEW.update(cell=float(cell), rot=float(rot))
    try:
        yield
    finally:
        _VIEW.update(old)


def _turn(p, angle):
    """Local points ``p`` (N, 2) or grids ``(x, y)`` turned by ``angle`` degrees about :data:`CENTRE`."""
    a = np.radians(angle)
    x, y = p[0] - CENTRE[0], p[1] - CENTRE[1]
    return (CENTRE[0] + np.cos(a) * x - np.sin(a) * y, CENTRE[1] + np.sin(a) * x + np.cos(a) * y)


def _grid():
    cell = _VIEW["cell"]
    w, h = int(round(SIZE[0] / cell)), int(round(SIZE[1] / cell))
    x = ORIGIN[0] + (np.arange(w) + 0.5) * cell
    y = ORIGIN[1] - (np.arange(h) + 0.5) * cell
    return np.meshgrid(x, y)


def _seg_distance(X, Y, line):
    """The distance of each cell from the polyline ``line`` (local metres) and the position along it (m)."""
    line = np.asarray(line, np.float64)
    best = np.full(X.shape, np.inf)
    along = np.zeros(X.shape)
    run = 0.0
    for a, b in zip(line[:-1], line[1:]):
        step = b - a
        L = float(np.hypot(*step))
        tau = np.clip(((X - a[0]) * step[0] + (Y - a[1]) * step[1]) / (L * L), 0.0, 1.0)
        d = np.hypot(X - a[0] - tau * step[0], Y - a[1] - tau * step[1])
        closer = d < best
        best = np.where(closer, d, best)
        along = np.where(closer, run + tau * L, along)
        run += L
    return best, along


def _local(X, Y):
    return X - ORIGIN[0], Y - (ORIGIN[1] - SIZE[1])  # local metres, origin at the bottom-left


def _smooth_noise(rng, shape, scale_cells, amplitude):
    """Smooth noise of ``amplitude`` m at most; ``scale_cells`` in cells of 0.05 m (the same metres on any cell)."""
    from scipy.ndimage import gaussian_filter

    n = gaussian_filter(rng.standard_normal(shape), scale_cells * CELL / _VIEW["cell"])
    return n / max(float(np.abs(n).max()), 1e-9) * amplitude


class _Builder:
    def __init__(self, seed: int, slope=(0.02, 0.01), undulation: float = 0.05):
        self.rng = np.random.default_rng(seed)
        X, Y = _grid()
        self.x, self.y = _turn(_local(X, Y), -_VIEW["rot"])  # the case's own frame, turned back
        self.ground = 100.0 + slope[0] * self.x + slope[1] * self.y
        if undulation:
            self.ground = self.ground + _smooth_noise(self.rng, X.shape, 60, undulation)
        self.ground = self.ground + 0.005 * self.rng.standard_normal(X.shape)  # gravel, the DEM's noise
        self.z = self.ground.copy()

    def wall(self, line, width, height, batter: float = 0.05, top_rough: float = 0.03):
        """A wall along ``line`` (local m): ``height`` m above the ground at its foot, the top uneven by stones
        (``top_rough`` m), the faces battered (``batter`` m out over its height)."""
        d, _ = _seg_distance(self.x, self.y, line)
        stones = _smooth_noise(self.rng, self.x.shape, 2.5, top_rough)
        half = width / 2.0
        top = self.ground + height + stones
        face = np.clip((half + batter - d) / max(batter, 1e-6), 0.0, 1.0)  # 1 on the top, 0 at the foot
        wall = self.ground + face * (top - self.ground)
        self.z = np.where(d <= half + batter, np.maximum(self.z, wall), self.z)

    def mound(self, line, side_point, height, reach, rough: float = 0.08):
        """Rubble against one face of a wall along ``line``: on the side of ``side_point`` from ``line``, ``height``
        m at the line falling to 0 at ``reach`` m, lumpy (``rough`` m)."""
        line = np.asarray(line, np.float64)
        d, _ = _seg_distance(self.x, self.y, line)
        a, b = line[0], line[-1]
        n = np.array([-(b - a)[1], (b - a)[0]])
        sgn = np.sign((np.asarray(side_point) - a) @ n)
        side = np.sign((self.x - a[0]) * n[0] + (self.y - a[1]) * n[1]) == sgn
        lumps = _smooth_noise(self.rng, self.x.shape, 4, rough)
        h = height * np.clip(1.0 - d / reach, 0.0, None) ** 1.5 + np.where(d < reach, lumps, 0.0)
        self.z = np.where(side & (d < reach), np.maximum(self.z, self.ground + h), self.z)

    def cut(self, mask, depth):
        """An excavated area (``mask``) ``depth`` m below the ground, its sides steep; what stands on the ground
        (walls, rubble) is left standing, its foot laid bare."""
        from scipy.ndimage import gaussian_filter

        m = gaussian_filter(mask.astype(np.float64), 1.0)
        bare = self.z <= self.ground + 0.02
        self.z = np.where(bare, self.z - depth * m, self.z)
        self.ground = self.ground - depth * m

    def block(self, cx, cy, side, height):
        """A tower: a square ``side`` m, ``height`` m high."""
        inside = (np.abs(self.x - cx) <= side / 2) & (np.abs(self.y - cy) <= side / 2)
        self.z = np.where(inside, np.maximum(self.z, self.ground + height), self.z)


def _world(p):
    p = np.asarray(p, np.float64)
    x, y = _turn((p[:, 0], p[:, 1]), _VIEW["rot"])
    return np.column_stack([x + ORIGIN[0], y + ORIGIN[1] - SIZE[1]])


def _case(name, b: _Builder, crest_local, click_in: float = 0.3):
    crest = np.asarray(crest_local, np.float64)
    ends = crest[[0, -1]].copy()
    u0 = (crest[1] - crest[0]) / np.hypot(*(crest[1] - crest[0]))
    u1 = (crest[-2] - crest[-1]) / np.hypot(*(crest[-2] - crest[-1]))
    ends[0] += u0 * click_in + np.array([-u0[1], u0[0]]) * 0.15  # a click near the end, a little off the crest
    ends[1] += u1 * click_in - np.array([-u1[1], u1[0]]) * 0.15
    return Case(name, b.z, _world(crest), _world(ends), _VIEW["cell"])


# ---------------------------------------------------------------- the cases
def free_wall(seed=0):
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 1.0)
    return _case("free", b, line)


def free_bent(seed=1):
    b = _Builder(seed)
    line = [(7, 10), (15, 16), (25, 19)]
    b.wall(line, 0.6, 0.8)
    return _case("free_bent", b, line)


def free_wide(seed=2):
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 1.2, 1.2)
    return _case("free_wide", b, line)


def parallel(gap, seed=3, other_height=1.4):
    """The target wall 0.7 m thick, 0.9 m high; another wall 0.8 m thick, taller, ``gap`` m from its face."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    y2 = 15 - 0.35 - gap - 0.4
    b.wall([(6, y2), (26, y2)], 0.8, other_height)
    return _case(f"parallel_{gap:.1f}", b, line)


def rubble(seed=4):
    """Collapse against the south face: 0.7 m high at the face, gone 2.5 m out, lumpy."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.mound([(6, 15 - 0.35), (26, 15 - 0.35)], (16, 10), 0.7, 2.5)
    b.wall(line, 0.7, 1.0)
    return _case("rubble", b, line)


def trench_edge(seed=5):
    """The wall on the edge of an excavated trench: the ground 1.3 m lower from 0.2 m beyond its north face."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    b.cut((b.y > 15 + 0.35 + 0.2) & (b.y < 15 + 0.55 + 4.0), 1.3)
    return _case("trench_edge", b, line)


def pit_cross(seed=6):
    """The wall crosses a pit 4 m across, 1.2 m deep, dug on both sides of it (its foot laid bare), and passes a
    smaller pit 0.8 m deep beside its south face."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    b.cut(np.hypot(b.x - 17.0, b.y - 15.0) < 2.0, 1.2)
    b.cut(np.hypot(b.x - 13.0, b.y - 13.6) < 1.5, 0.8)
    return _case("pit_cross", b, line)


def corner(seed=7):
    """The target wall meets a cross wall in a T at its middle and an L at its east end, a tower 3 m square beside
    its west end."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.7, 0.9)
    b.wall([(16, 15), (16, 8)], 0.7, 1.1)
    b.wall([(24, 15), (24, 23)], 0.8, 1.2)
    b.block(9.5, 17.6, 3.0, 2.5)
    return _case("corner", b, line)


def low_wall(seed=8):
    """A wall 0.2 m high, 0.6 m thick, on uneven ground (undulations of 0.25 m over a few metres)."""
    b = _Builder(seed, undulation=0.25)
    line = [(8, 15), (24, 15)]
    b.wall(line, 0.6, 0.2, top_rough=0.02)
    return _case("low", b, line)


def tower_beside(seed=10):
    """An oval tower 8 x 6 m, 3 m high, built against the north face of the wall in its middle."""
    b = _Builder(seed)
    line = [(6, 15), (26, 15)]
    b.wall(line, 0.7, 0.9)
    inside = ((b.x - 16) / 4.0) ** 2 + ((b.y - 18.35) / 3.0) ** 2 <= 1.0
    b.z = np.where(inside, np.maximum(b.z, b.ground + 3.0), b.z)
    return _case("tower_beside", b, line)


def robbed(seed=11):
    """A wall robbed in stretches: its height from 1.1 m down to 0.25 m and up again along it."""
    b = _Builder(seed)
    xs = np.linspace(8, 24, 9)
    for x0, x1, h in zip(xs[:-1], xs[1:], (1.1, 0.8, 0.3, 0.25, 0.6, 1.0, 0.4, 0.9)):
        b.wall([(x0, 15), (x1, 15)], 0.7, h, top_rough=0.05)
    return _case("robbed", b, [(8, 15), (24, 15)])


def crowded(seed=9):
    """All at once, as on the site: a parallel wall 1 m off, rubble against the other face, a trench at one end, a
    tower at the other."""
    b = _Builder(seed)
    line = [(8, 15), (24, 15)]
    b.mound([(6, 15 + 0.35), (26, 15 + 0.35)], (16, 20), 0.6, 2.0)
    b.wall(line, 0.7, 0.9)
    b.cut((b.x > 18) & (b.x < 23) & (b.y < 15 - 0.35 - 0.2) & (b.y > 15 - 0.35 - 4), 1.2)
    b.wall([(6, 13.25), (17, 13.25)], 0.8, 1.3)
    b.block(25.8, 16.5, 3.0, 2.5)
    return _case("crowded", b, line)


def all_cases() -> list[Case]:
    return [free_wall(), free_bent(), free_wide(), parallel(0.5), parallel(1.0), parallel(1.5), rubble(),
            trench_edge(), pit_cross(), corner(), tower_beside(), robbed(), low_wall(), crowded()]


# ---------------------------------------------------------------- writing and measuring
def chunk_of(case: Case, folder) -> SimpleNamespace:
    """A minimal chunk with the case's DEM as its product (no orthophoto)."""
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{case.name}_{case.cell:.2f}_{abs(hash(case.crest.tobytes())) % 10 ** 8}"
    path = write_geotiff(folder / f"{name}.tif", case.dem.astype(np.float32), ORIGIN, case.cell, "EPSG:32633")
    return SimpleNamespace(dem_path=path, orthomosaic_path=None)


def _sample(line, step=0.02):
    line = np.asarray(line, np.float64).reshape(-1, 2)
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))])
    at = np.linspace(0.0, run[-1], max(2, int(np.ceil(run[-1] / step)) + 1))
    return np.column_stack([np.interp(at, run, line[:, 0]), np.interp(at, run, line[:, 1])])


def _distance_to(line_xy, points):
    line = np.asarray(line_xy, np.float64).reshape(-1, 2)
    best = np.full(len(points), np.inf)
    for a, b in zip(line[:-1], line[1:]):
        step = b - a
        tau = np.clip((points - a) @ step / max(float(step @ step), 1e-300), 0.0, 1.0)
        best = np.minimum(best, np.hypot(*(points - a - tau[:, None] * step).T))
    return best


def score(traced, crest, tolerance: float = 0.10, trim: float = 0.5):
    """``(share, mean)``: the share of the true crest (sampled every 2 cm, ``trim`` m left out at each end, where the
    clicks are) within ``tolerance`` of the traced line, and the mean distance of those samples from it."""
    s = _sample(crest)
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(s, axis=0).T))])
    s = s[(run >= trim) & (run <= run[-1] - trim)]
    d = _distance_to(traced, s)
    return float((d <= tolerance).mean()), float(d.mean())


def score_union(lines, crest, tolerance: float = 0.10, trim: float = 0.5):
    """:func:`score` against several lines at once (the proposals of a plan): each crest sample measured from the
    nearest of them; ``(0, inf)`` without lines."""
    if not lines:
        return 0.0, float("inf")
    s = _sample(crest)
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(s, axis=0).T))])
    s = s[(run >= trim) & (run <= run[-1] - trim)]
    d = np.min([_distance_to(line, s) for line in lines], axis=0)
    return float((d <= tolerance).mean()), float(np.minimum(d, 5.0).mean())


def coarsened(case: Case, factor: int) -> Case:
    """``case`` with its DEM averaged over blocks of ``factor`` x ``factor`` cells (a large plan read coarser)."""
    h, w = (np.asarray(case.dem.shape) // factor) * factor
    dem = case.dem[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))
    return Case(f"{case.name}_x{factor}", dem, case.crest, case.clicks, case.cell * factor)


def holed(case: Case, holes: int = 40, seed: int = 0) -> Case:
    """``case`` with ``holes`` discs of no data (NaN, 0.1-0.3 m across) around the wall and a strip of no data 1 m
    long across it, from 0.5 m to 1.5 m along its crest."""
    rng = np.random.default_rng(seed)
    dem = case.dem.copy()
    h, w = dem.shape
    rows, cols = np.mgrid[0:h, 0:w]
    lo, hi = case.crest.min(axis=0) - 2.0, case.crest.max(axis=0) + 2.0  # holes around the wall, where they matter
    for _ in range(holes):
        x, y = rng.uniform(lo[0], hi[0]), rng.uniform(lo[1], hi[1])
        r = rng.uniform(0.05, 0.15) / case.cell
        c0, r0 = (x - ORIGIN[0]) / case.cell, (ORIGIN[1] - y) / case.cell
        dem[(rows - r0) ** 2 + (cols - c0) ** 2 <= r * r] = np.nan
    a = case.crest[0]
    u = (case.crest[1] - a) / np.hypot(*(case.crest[1] - a))
    x = ORIGIN[0] + (cols + 0.5) * case.cell - a[0]
    y = ORIGIN[1] - (rows + 0.5) * case.cell - a[1]
    along, across = x * u[0] + y * u[1], -x * u[1] + y * u[0]
    dem[(along > 0.5) & (along < 1.5) & (np.abs(across) < 1.0)] = np.nan  # a strip of no data across the wall
    return Case(f"{case.name}_holed", dem, case.crest, case.clicks, case.cell)
