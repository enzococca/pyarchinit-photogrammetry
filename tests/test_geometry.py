import numpy as np

from pyarchinit_photogrammetry.core import geometry


def test_umeyama_recovers_similarity(rng):
    src = rng.normal(size=(20, 3))
    rot = geometry.rotation_from_euler_ypr(30, -10, 5)
    m = geometry.compose_similarity(2.5, rot, [100.0, -50.0, 3.0])
    dst = geometry.apply(m, src)
    est = geometry.umeyama(src, dst)
    assert np.allclose(est, m, atol=1e-9)
    s, r, t = geometry.decompose_similarity(est)
    assert np.isclose(s, 2.5)


def test_triangulate_two_views():
    p = np.array([0.3, -0.2, 5.0])
    views = [geometry.PinholeView(geometry.look_at([x, 0, 0], [0, 0, 5], up=(0, -1, 0))) for x in (-1.0, 1.0, 0.2)]
    obs = []
    for v in views:
        pc = v.cam_from_world[:3, :3] @ p + v.cam_from_world[:3, 3]
        obs.append(pc[:2] / pc[2])
    est = geometry.triangulate(views, np.asarray(obs))
    assert np.allclose(est, p, atol=1e-8)


def test_triangulate_drops_non_finite_observations():
    """A measurement whose lens model cannot be inverted (``Sensor.unproject`` gives NaN at the
    borders of a strong calibration) is left out instead of stopping the SVD; fewer than two
    finite rays are a clear error, not ``LinAlgError: SVD did not converge``."""
    import pytest

    p = np.array([0.3, -0.2, 5.0])
    views = [geometry.PinholeView(geometry.look_at([x, 0, 0], [0, 0, 5], up=(0, -1, 0))) for x in (-1.0, 1.0, 0.2)]
    obs = []
    for v in views:
        pc = v.cam_from_world[:3, :3] @ p + v.cam_from_world[:3, 3]
        obs.append(pc[:2] / pc[2])
    obs[2] = np.array([np.nan, np.nan])
    assert np.allclose(geometry.triangulate(views, np.asarray(obs)), p, atol=1e-8)
    assert np.allclose(geometry.triangulate(views, np.asarray(obs), refine=False), p, atol=1e-8)
    obs[1] = np.array([0.1, np.inf])
    with pytest.raises(ValueError, match="finite"):
        geometry.triangulate(views, np.asarray(obs))
