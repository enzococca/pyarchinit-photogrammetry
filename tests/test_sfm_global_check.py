"""The check of a global alignment against the GPS of the photos (engine.sfm.fits_references):
a block assembled from parts at different scales fits no similarity, a sound one fits within
the noise of the GPS."""
import numpy as np

from pyarchinit_photogrammetry.engine.sfm import fits_references


def _survey(n: int = 300, seed: int = 0):
    """GPS positions of a flight over 400 x 300 m and the same centres in an internal frame
    (rotated, 1/72 of the scale, shifted), with 2 m of GPS noise."""
    rng = np.random.default_rng(seed)
    gps = np.column_stack([rng.uniform(0, 400, n), rng.uniform(0, 300, n), 100 + rng.uniform(-3, 3, n)])
    a = np.radians(35.0)
    rot = np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])
    internal = (rot @ (gps - [200.0, 150.0, 100.0]).T).T / 72.0 + [3.0, -2.0, 1.0]
    noisy = gps + rng.normal(0.0, 2.0, gps.shape)
    return internal, noisy


def test_a_sound_block_fits_its_gps():
    internal, gps = _survey()
    fits, residual = fits_references(internal, gps, np.full(len(gps), 5.0))
    assert fits and residual < 5.0


def test_parts_at_different_scales_do_not_fit():
    internal, gps = _survey()
    west = gps[:, 0] < 200.0  # half of the block at 40 % of its scale, as the global mapper left Tell Tuqan
    part = internal[west]
    internal = internal.copy()
    internal[west] = (part - part.mean(0)) * 0.4 + part.mean(0)
    fits, residual = fits_references(internal, gps, np.full(len(gps), 5.0))
    assert not fits and residual > 15.0  # tolerance: 3 x 5 m


def test_few_or_close_references_cannot_judge():
    internal, gps = _survey(n=8)
    fits, residual = fits_references(internal, gps, np.full(8, 5.0))
    assert fits and np.isnan(residual)
    internal, gps = _survey()
    small = gps / 100.0  # a 4 x 3 m object photographed with a 5 m GPS: nothing to compare
    fits, residual = fits_references(internal, small, np.full(len(gps), 5.0))
    assert fits and np.isnan(residual)
