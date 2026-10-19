"""Single-cell spikes of a DEM gridded at one point per cell are taken back to their neighbours;
real shapes - a wall a few cells wide, a slope - stay as they are."""
from __future__ import annotations

import numpy as np

from pyarchinit_photogrammetry.engine.dem import remove_spikes


def test_spikes_go_walls_stay():
    rng = np.random.default_rng(0)
    res = 0.2
    rows, cols = np.mgrid[0:200, 0:200]
    surface = 100.0 + 0.05 * cols + 0.02 * rows + rng.normal(0, 0.01, (200, 200))
    surface[90:110, 60:63] += 1.0  # a wall three cells wide, a metre high
    surface[150, 20] = np.nan  # a hole stays a hole
    dem = surface.copy()
    lattice = [(r, c) for r in range(2, 198, 5) for c in range(2, 198, 5)]  # stray points, apart
    picked = [lattice[i] for i in rng.choice(len(lattice), 300, replace=False)]
    spikes = np.zeros(dem.shape, bool)
    for r, c in picked:
        spikes[r, c] = True
    dem[spikes] += rng.choice([-1.5, 2.0], spikes.sum())
    dem[150, 20] = np.nan
    out, n = remove_spikes(dem, res)
    wall = np.zeros(dem.shape, bool)
    wall[90:110, 60:63] = True
    assert n == spikes.sum() - np.isnan(dem[spikes]).sum()
    assert np.nanmax(np.abs(out - surface)) < 0.1  # the spikes are back on the ground
    assert np.allclose(out[wall & ~spikes], surface[wall & ~spikes])  # corners and edges of the wall too
    assert np.isnan(out[150, 20])
